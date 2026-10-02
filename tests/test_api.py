"""End to end: the FastAPI app backed by the FakePortal and a temporary SQLite index."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import math
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fake_portal import BASE_URL, EMAIL, PASSWORD, FakePortal
from pydantic import ValidationError

from urja_api import sync as sync_module
from urja_api.api.app import create_app
from urja_api.api.deps import Services
from urja_api.config import Settings
from urja_api.domain.normalize import IST, RawReading
from urja_api.portal import client as client_module
from urja_api.portal.errors import PortalAuthError
from urja_api.portal.ratelimit import TokenBucket

PROBLEM_JSON = "application/problem+json"
# The fixture's DT list has 40 transformers, but its 20 meters hang off only 11 of them.
UNSERVED = "29 transformers on the portal's list have no meters and are not served"
# The fixture data ends on 2026-06-30 (half-hourly meters: 2026-06-05), so the `stale` rule
# is judged against a pinned clock rather than the day the suite happens to run.
AFTER_THE_DATA = datetime(2026, 10, 1, tzinfo=UTC)
HALF_HOURLY = ["J100000", "J100001", "J100002", "J100004", "J100006", "J100010", "J100011"]


@dataclass
class Api:
    http: httpx.AsyncClient
    portal: FakePortal
    services: Services
    app: Any

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self.http.get(path, params=params)
        assert resp.status_code == 200, resp.text
        return resp.json()

    async def problem(self, path: str, status: int, params: dict[str, Any] | None = None, **kwargs: Any) -> dict:
        resp = await self.http.get(path, params=params, **kwargs)
        assert resp.status_code == status, resp.text
        assert resp.headers["content-type"] == PROBLEM_JSON
        body = resp.json()
        assert body["status"] == status
        return body


@pytest.fixture
async def start_api(tmp_path, make_portal) -> AsyncIterator[Callable[..., Any]]:
    """Start the app (lifespan included) against a FakePortal; optionally run the first sync."""
    counter = itertools.count()
    async with contextlib.AsyncExitStack() as stack:

        async def start(*, sync: bool = True, **overrides: Any) -> Api:
            portal = make_portal()
            settings = {
                "portal_base_url": BASE_URL,
                "portal_email": EMAIL,
                "portal_password": PASSWORD,
                "db_path": tmp_path / f"urja-{next(counter)}.sqlite3",
                "api_key": None,
                "warm_readings": False,
                "portal_rate_limit_per_minute": 60_000,  # the fake has its own limit; don't pace tests
                "portal_rate_limit_burst": 1_000,
                **overrides,
            }
            app = create_app(
                Settings(_env_file=None, **settings),
                transport=httpx.ASGITransport(app=portal.app),
                background_sync=False,
            )
            await stack.enter_async_context(app.router.lifespan_context(app))
            services: Services = app.state.services
            if sync:
                await services.sync.sync_reference_data()
            http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")
            await stack.enter_async_context(http)
            return Api(http, portal, services, app)

        yield start


@pytest.fixture
async def api(start_api) -> Api:
    return await start_api()


@pytest.fixture
async def warm_api(api) -> Api:
    """The app with every fixture meter's readings cached (what the background warm-up does)."""
    await api.services.sync.warm_all_readings()
    return api


@pytest.fixture
def no_sync_cooldown(monkeypatch) -> None:
    """Let every POST /v1/sync run a sync, however soon after the previous one."""
    monkeypatch.setattr(sync_module, "MANUAL_SYNC_MIN_INTERVAL_S", 0)


def freeze(api: Api, at: datetime) -> None:
    """Pin the one clock the service judges time by (staleness, ages, cache TTLs)."""
    api.services.sync.clock = lambda: at


def ids(page: dict) -> list[str]:
    return [item["meter_id"] for item in page["items"]]


def walk(nodes: list[dict]) -> Iterator[dict]:
    for node in nodes:
        yield node
        yield from walk(node["children"])


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    (lat1, lng1), (lat2, lng2) = (tuple(map(math.radians, p)) for p in (a, b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(h))


# ----------------------------------------------------------------------------- system


async def test_healthz(api):
    assert await api.get("/healthz") == {"status": "ok"}


async def test_status_after_the_first_sync(api):
    status = await api.get("/v1/status")
    assert status["index_ready"] is True
    reference = status["reference_data"]
    assert (reference["source"], reference["stale"]) == ("export", False)
    assert reference["last_run"]["status"] == "ok"
    assert (reference["last_run"]["meter_count"], reference["last_run"]["transformer_count"]) == (20, 11)
    assert (status["portal"]["base_url"], status["portal"]["logins"]) == (BASE_URL, 1)


async def test_index_not_ready_before_the_first_sync(start_api):
    api = await start_api(sync=False)
    resp = await api.http.get("/v1/meters")
    assert resp.status_code == 503
    assert resp.headers["content-type"] == PROBLEM_JSON
    assert resp.headers["retry-after"] == "5"
    assert resp.json()["code"] == "index_not_ready"
    assert (await api.get("/v1/status"))["index_ready"] is False


async def test_index_not_ready_says_why_the_first_sync_failed(start_api):
    api = await start_api(sync=False, portal_password="not-the-password")
    with pytest.raises(PortalAuthError):
        await api.services.sync.sync_reference_data()
    api.services.sync.next_sync_at = time.monotonic() + 42  # what the sync loop schedules after a failure
    resp = await api.http.get("/v1/meters")
    assert resp.status_code == 503
    assert 40 <= int(resp.headers["retry-after"]) <= 42
    body = resp.json()
    assert body["code"] == "index_not_ready"
    assert "first sync with the portal failed (PortalAuthError: login failed" in body["detail"]


async def test_manual_sync(api, monkeypatch):
    exports = api.portal.calls["/portal/export"]
    resp = await api.http.post("/v1/sync")
    assert resp.status_code == 200
    assert resp.json() == {"source": "export", "meter_count": 20, "transformer_count": 11, "warnings": [UNSERVED]}
    assert api.portal.calls["/portal/export"] == exports  # a sync just ran: its result is returned

    monkeypatch.setattr(sync_module, "MANUAL_SYNC_MIN_INTERVAL_S", 0)
    resp = await api.http.post("/v1/sync")
    assert api.portal.calls["/portal/export"] == exports + 1
    assert resp.json()["warnings"] == [UNSERVED, "portal data unchanged since the last sync; snapshot kept as is"]


async def test_concurrent_manual_syncs_share_one_run(api, no_sync_cooldown):
    exports = api.portal.calls["/portal/export"]
    responses = await asyncio.gather(*(api.http.post("/v1/sync") for _ in range(5)))
    assert [r.status_code for r in responses] == [200] * 5
    assert api.portal.calls["/portal/export"] == exports + 1


async def test_manual_sync_reports_the_crawl_fallback(api, no_sync_cooldown):
    near = {"near": "26.938961,75.830957", "radius_km": 50}

    async def located() -> list[tuple[str, dict, float]]:
        page = await api.get("/v1/meters", near)
        return [(m["meter_id"], m["location"], m["distance_km"]) for m in page["items"]]

    before = await located()
    assert len(before) == 20
    api.portal.signature_tolerance_s = -1  # every export signature is rejected from now on
    resp = await api.http.post("/v1/sync")
    assert resp.status_code == 200
    body = resp.json()
    assert (body["source"], body["meter_count"], body["transformer_count"]) == ("crawl", 20, 11)
    assert body["warnings"][0].startswith("export unusable, used detail-page crawl")
    # Detail pages carry no coordinates: each meter keeps its last known location.
    assert await located() == before


async def test_an_export_outage_keeps_the_snapshot_instead_of_crawling(start_api, no_sync_cooldown):
    api = await start_api(portal_max_wait_s=0.3)
    pages = api.portal.calls["/meters/J100000/__data.json"]
    api.portal.fail_paths["/portal/export"] = 503
    resp = await api.http.post("/v1/sync")
    assert (resp.status_code, resp.json()["code"]) == (503, "upstream_unavailable")
    assert api.portal.calls["/meters/J100000/__data.json"] == pages  # no crawl
    assert (await api.get("/v1/meters/J100000"))["location"] is not None


async def test_a_rejected_manual_sync_is_a_problem_response(api, no_sync_cooldown):
    # A sync the shrink guard refuses is the portal misbehaving: answer with problem+json, not
    # a bare text/plain 500 (an unhandled SyncRejected), and keep serving the last snapshot.
    api.portal.meters = api.portal.meters[:5]  # the export suddenly lists a quarter of the meters
    resp = await api.http.post("/v1/sync")
    assert resp.status_code == 502, resp.text
    assert resp.headers["content-type"] == PROBLEM_JSON
    assert resp.json()["code"] == "sync_rejected"
    assert (await api.get("/v1/meters"))["total"] == 20  # the previous snapshot is still served


async def test_status_before_the_first_sync(start_api):
    status = await (await start_api(sync=False)).get("/v1/status")
    assert status["reference_data"] == {
        "last_success_at": None,
        "age_s": None,
        "stale": True,
        "source": None,
        "interval_s": 900,
        "last_run": None,
    }
    cache = status["readings_cache"]
    assert (cache["meter_count"], cache["oldest_fetch"], cache["last_reading_at"], cache["ttl_s"]) == (
        0,
        None,
        None,
        900,
    )
    assert cache["warmup"] == {
        "state": "idle",
        "done": 0,
        "total": 0,
        "failed": 0,
        "started_at": None,
        "finished_at": None,
        "errors": {},
    }
    assert status["portal"] == {
        "base_url": BASE_URL,
        "logins": 0,
        "requests": 0,
        "retries": 0,
        "rate_limited": 0,
        "upstream_errors": 0,
        "last_error": None,
        "last_success_at": None,
        "session_expires_at": None,
        "clock_skew_s": 0.0,
    }


async def test_status_after_the_warm_up(warm_api):
    status = await warm_api.get("/v1/status")
    reference = status["reference_data"]
    assert set(reference) == {"last_success_at", "age_s", "stale", "source", "interval_s", "last_run"}
    run = reference["last_run"]
    assert {k: run[k] for k in ("status", "source", "meter_count", "transformer_count", "warnings", "error")} == {
        "status": "ok",
        "source": "export",
        "meter_count": 20,
        "transformer_count": 11,
        "warnings": [UNSERVED],
        "error": None,
    }
    assert datetime.fromisoformat(run["started_at"]) <= datetime.fromisoformat(run["finished_at"])
    assert run["finished_at"] == reference["last_success_at"]

    cache = status["readings_cache"]
    assert {k: cache[k] for k in ("meter_count", "first_reading_at", "last_reading_at", "ttl_s")} == {
        "meter_count": 20,
        "first_reading_at": "2026-06-01T00:00:00+05:30",
        "last_reading_at": "2026-06-30T00:00:00+05:30",
        "ttl_s": 900,
    }
    assert datetime.fromisoformat(cache["oldest_fetch"]) <= datetime.fromisoformat(cache["newest_fetch"])
    warmup = cache["warmup"]
    assert {k: warmup[k] for k in ("state", "done", "total", "failed", "errors")} == {
        "state": "done",
        "done": 20,
        "total": 20,
        "failed": 0,
        "errors": {},
    }
    assert datetime.fromisoformat(warmup["started_at"]) <= datetime.fromisoformat(warmup["finished_at"])

    portal = status["portal"]
    counters = (portal["logins"], portal["rate_limited"], portal["upstream_errors"], portal["last_error"])
    assert counters == (1, 0, 0, None)
    assert portal["requests"] == 2 + 1 + 1 + 20  # two DT pages, the signing key, the export, one series per meter
    assert portal["session_expires_at"] is not None


async def test_reference_data_is_stale_after_two_missed_syncs(api):
    synced = datetime.fromisoformat((await api.get("/v1/status"))["reference_data"]["last_success_at"])
    for elapsed, stale in ((2 * 900, False), (2 * 900 + 1, True)):
        freeze(api, synced + timedelta(seconds=elapsed))
        reference = (await api.get("/v1/status"))["reference_data"]
        assert (reference["age_s"], reference["stale"]) == (elapsed, stale)


@pytest.mark.parametrize(
    ("break_portal", "status", "code", "retry_after"),
    [
        pytest.param(lambda p: setattr(p, "rate_limit", 0), 503, "upstream_rate_limited", "4", id="rate-limited"),
        pytest.param(lambda p: p.fail_next.append(503), 503, "upstream_unavailable", "30", id="server-error"),
        pytest.param(lambda p: p.fail_next.append(200), 502, "upstream_protocol_error", None, id="not-json"),
    ],
)
async def test_upstream_failures_without_a_cached_copy(start_api, monkeypatch, break_portal, status, code, retry_after):
    monkeypatch.setattr(client_module, "random", SimpleNamespace(uniform=lambda low, high: 1.0))  # no jitter
    api = await start_api(portal_max_wait_s=0.4)  # the first back-off (0.5 s, or 4 s after a 429) won't fit
    break_portal(api.portal)
    resp = await api.http.get("/v1/meters/J100000/readings")  # nothing cached for this meter yet
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"] == PROBLEM_JSON
    assert resp.json()["code"] == code
    assert resp.headers.get("retry-after") == retry_after


async def test_upstream_auth_failure_is_a_bad_gateway(start_api):
    api = await start_api(sync=False, portal_password="not-the-password")
    resp = await api.http.post("/v1/sync")
    assert resp.status_code == 502
    assert resp.headers["content-type"] == PROBLEM_JSON
    assert resp.json()["code"] == "upstream_auth_failed"
    run = (await api.get("/v1/status"))["reference_data"]["last_run"]
    assert (run["status"], run["error"].split(":")[0]) == ("failed", "PortalAuthError")


# ----------------------------------------------------------------------------- meters


async def test_filters(api, export_records):
    def expected(predicate: Callable[[dict], bool]) -> list[str]:
        return sorted(r["meterId"] for r in export_records if predicate(r))

    async def matching(**params: Any) -> list[str]:
        return ids(await api.get("/v1/meters", {"limit": 500, **params}))

    assert await matching(status="decommissioned") == ["J100000", "J100006", "J100011", "J100100"]
    assert await matching(status=["faulty", "decommissioned"]) == expected(
        lambda r: r["installStatus"] in ("Faulty", "Decommissioned")
    )
    assert await matching(make="l&t") == ["J100001", "J100002"]  # case-insensitive
    assert await matching(make="Genus", status="installed") == ["J100400", "J100401", "J100402"]
    assert await matching(phase="three", installation_type="ct_operated") == expected(
        lambda r: r["phaseType"] == "three" and r["installType"] == "CT Operated"
    )


async def test_network_filters_use_the_repaired_codes(api):
    async def matching(**params: Any) -> list[str]:
        return ids(await api.get("/v1/meters", {"limit": 500, **params}))

    assert await matching(circle="C-06") == ["J100011", "J100051"]  # J100011 reported no circle code
    assert await matching(substation="ss-16") == ["J100073", "J100153"]  # likewise, lower case accepted
    assert await matching(division="D-01") == ["J100000", "J100010", "J100040", "J100100", "J100200"]
    assert await matching(feeder="F-003") == ["J100002", "J100162"]
    assert await matching(transformer="DT-007") == ["J100006", "J100400", "J100401", "J100402"]
    assert await matching(zone="Z-02", transformer="DT-005") == ["J100004"]


async def test_search_treats_wildcards_literally(api, export_records):
    async def search(q: str) -> list[str]:
        return ids(await api.get("/v1/meters", {"q": q, "limit": 500}))

    assert await search("j10000") == ["J100000", "J100001", "J100002", "J100004", "J100006"]
    assert await search("se3") == sorted(r["meterId"] for r in export_records if "se3" in r["serialNo"].lower())
    assert await search("%") == []  # the portal's own search would return every meter
    assert await search("_") == []


async def test_has_issues(api):
    flagged = await api.get("/v1/meters", {"has_issues": "true"})
    assert ids(flagged) == ["J100011", "J100153", "J100162", "J100218", "J100400", "J100401", "J100402"]
    assert (await api.get("/v1/meters", {"has_issues": "false"}))["total"] == 13


async def test_pagination(api):
    everything = ids(await api.get("/v1/meters", {"limit": 500}))
    first = await api.get("/v1/meters", {"limit": 8})
    last = await api.get("/v1/meters", {"limit": 8, "offset": 16})
    assert everything == sorted(everything) and len(everything) == 20
    assert (first["total"], first["limit"], first["offset"]) == (20, 8, 0)
    assert (last["total"], last["limit"], last["offset"]) == (20, 8, 16)
    assert ids(first) == everything[:8]
    assert ids(last) == everything[16:]
    await api.problem("/v1/meters", 422, {"limit": 0})


async def test_near_orders_by_distance_within_the_radius(api, export_records):
    centre = (26.938961, 75.830957)  # J100000
    distances = sorted((haversine_km(centre, (r["geo"]["lat"], r["geo"]["lng"])), r["meterId"]) for r in export_records)
    within = [(d, meter_id) for d, meter_id in distances if d <= 6]
    page = await api.get("/v1/meters", {"near": "26.938961,75.830957", "radius_km": 6})
    assert ids(page) == [meter_id for _, meter_id in within] == ["J100000", "J100218", "J100100", "J100011"]
    assert [item["distance_km"] for item in page["items"]] == pytest.approx([d for d, _ in within], abs=1e-3)
    assert page["total"] == 4


async def test_near_includes_meters_at_the_edge_of_the_radius(api):
    # J100000 sits 1.999 km due north of the search point, so a 2 km radius must include it.
    meter = (26.938961, 75.830957)
    point = (meter[0] - 1.999 / (math.pi * 6371.0088 / 180), meter[1])
    assert haversine_km(point, meter) == pytest.approx(1.999)
    page = await api.get("/v1/meters", {"near": f"{point[0]},{point[1]}", "radius_km": 2})
    assert "J100000" in ids(page)


async def test_bbox(api, export_records):
    min_lng, min_lat, max_lng, max_lat = 75.80, 26.90, 75.86, 27.00
    expected = sorted(
        r["meterId"]
        for r in export_records
        if min_lat <= r["geo"]["lat"] <= max_lat and min_lng <= r["geo"]["lng"] <= max_lng
    )
    page = await api.get("/v1/meters", {"bbox": f"{min_lng},{min_lat},{max_lng},{max_lat}", "limit": 500})
    assert ids(page) == expected and len(expected) == 5
    assert all(item["distance_km"] is None for item in page["items"])


@pytest.mark.parametrize(
    "params",
    [
        {"near": "26.9"},
        {"near": "91,75.8"},
        {"near": "nan,nan"},
        {"near": "26.9,75.8", "radius_km": 0},
        {"bbox": "75.9,26.9,75.8,27.0"},
        {"bbox": "nan,nan,nan,nan"},
        {"bbox": "0,100,1,101"},
        {"bbox": "-inf,26.9,inf,27.0"},
    ],
    ids=[
        "one-number",
        "latitude-out-of-range",
        "near-nan",
        "zero-radius",
        "inverted-bbox",
        "bbox-nan",
        "bbox-latitude-out-of-range",
        "bbox-infinite",
    ],
)
async def test_invalid_geo_queries(api, params):
    assert (await api.problem("/v1/meters", 422, params))["code"] == "validation_error"


async def test_meter_detail(api):
    meter = await api.get("/v1/meters/j100400")  # any case
    assert meter["meter_id"] == "J100400"
    assert (meter["status"], meter["phase"], meter["installation_type"]) == ("installed", "three", "ct_operated")
    assert meter["location"] == {"latitude": 26.882703, "longitude": 75.740158}
    assert meter["network"]["transformer"] == {"code": "DT-007", "name": "Sanganer DT 7"}
    assert meter["transformer"] == {
        "code": "DT-007",
        "name": "Sanganer DT 7",
        "feeder_code": "F-007",
        "capacity_kva": 63,
    }
    [issue] = meter["data_issues"]
    assert (issue["code"], issue["level"], issue["reported"], issue["resolved"]) == (
        "stale_name",
        "transformer",
        "Old Malviya Nagar Xfmr",
        "Sanganer DT 7",
    )


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/v1/meters/J999999", "meter_not_found"),
        ("/v1/meters/not-a-meter-id", "meter_not_found"),
        ("/v1/meters/J999999/readings", "meter_not_found"),
        ("/v1/transformers/DT-999", "transformer_not_found"),
        ("/v1/network/division/D-99", "network_node_not_found"),
        ("/v1/no-such-endpoint", "not_found"),
    ],
)
async def test_not_found_is_problem_json(api, path, code):
    body = await api.problem(path, 404)
    assert body["code"] == code
    assert (body["type"], body["title"]) == ("about:blank", "Not Found")
    assert body["detail"]


async def test_sort(api):
    everything = (await api.get("/v1/meters", {"limit": 500}))["items"]

    async def order(sort: str) -> list[str]:
        return ids(await api.get("/v1/meters", {"sort": sort, "limit": 500}))

    most_issues_first = await order("-issues")
    assert most_issues_first == [
        m["meter_id"] for m in sorted(everything, key=lambda m: (-m["data_issue_count"], m["meter_id"]))
    ]
    assert most_issues_first[:7] == ["J100011", "J100153", "J100162", "J100218", "J100400", "J100401", "J100402"]
    assert await order("make") == [m["meter_id"] for m in sorted(everything, key=lambda m: (m["make"], m["meter_id"]))]
    assert await order("-meter_id") == sorted((m["meter_id"] for m in everything), reverse=True)
    pages = [ids(await api.get("/v1/meters", {"sort": "-issues", "limit": 6, "offset": n})) for n in range(0, 20, 6)]
    assert list(itertools.chain.from_iterable(pages)) == most_issues_first  # paging follows the same order


async def test_invalid_sort(api):
    for sort in ("distance", "-distance", "latitude", "issues-"):  # distance needs near=; the others don't exist
        body = await api.problem("/v1/meters", 422, {"sort": sort})
        assert (body["code"], body["errors"][0]["location"]) == ("validation_error", ["query", "sort"]), sort


async def test_near_with_another_sort_key(api):
    near = {"near": "26.938961,75.830957", "radius_km": 6}
    by_id = await api.get("/v1/meters", {**near, "sort": "meter_id"})
    assert ids(by_id) == ["J100000", "J100011", "J100100", "J100218"]
    assert all(item["distance_km"] is not None for item in by_id["items"])
    farthest_first = await api.get("/v1/meters", {**near, "sort": "-distance"})
    assert ids(farthest_first) == ["J100011", "J100100", "J100218", "J100000"]


async def test_list_items_carry_issue_count_and_reading_interval(api):
    before = {m["meter_id"]: m for m in (await api.get("/v1/meters", {"limit": 500}))["items"]}
    flagged = {meter_id: m["data_issue_count"] for meter_id, m in before.items() if m["data_issue_count"]}
    assert flagged == dict.fromkeys(["J100011", "J100153", "J100162", "J100218", "J100400", "J100401", "J100402"], 1)
    assert len((await api.get("/v1/meters/J100400"))["data_issues"]) == flagged["J100400"]
    assert all(m["reading_interval_minutes"] is None for m in before.values())  # nothing cached yet
    assert (await api.get("/v1/meters", {"interval_minutes": 30}))["total"] == 0

    await api.services.sync.warm_all_readings()
    half_hourly = await api.get("/v1/meters", {"interval_minutes": 30})
    assert ids(half_hourly) == HALF_HOURLY
    assert {m["reading_interval_minutes"] for m in half_hourly["items"]} == {30}
    daily = await api.get("/v1/meters", {"interval_minutes": 1440, "limit": 500})
    assert daily["total"] == 13
    assert {m["reading_interval_minutes"] for m in daily["items"]} == {1440}
    decommissioned = await api.get("/v1/meters", {"interval_minutes": 30, "status": "decommissioned"})
    assert ids(decommissioned) == ["J100000", "J100006", "J100011"]


async def test_meter_detail_reports_what_the_readings_cache_holds(api):
    assert (await api.get("/v1/meters/J100000"))["readings_coverage"] is None  # not cached yet
    fetched_at = (await api.get("/v1/meters/J100000/readings"))["freshness"]["fetched_at"]
    assert (await api.get("/v1/meters/J100000"))["readings_coverage"] == {
        "interval_minutes": 30,
        "count": 240,
        "first_reading_at": "2026-06-01T00:00:00+05:30",
        "last_reading_at": "2026-06-05T23:30:00+05:30",
        "fetched_at": fetched_at,
    }


# ----------------------------------------------------------------------------- readings & consumption


async def test_readings_default_to_the_last_week_of_data(api, stored):
    body = await api.get("/v1/meters/J100040/readings")
    rows = stored("J100040")  # daily, up to 30/06
    assert (body["start"], body["end"]) == ("2026-06-23T00:00:00+05:30", "2026-06-30T00:00:00+05:30")
    assert body["last_reading_at"] == "2026-06-30T00:00:00+05:30"
    assert [r["timestamp"] for r in body["readings"]] == [f"2026-06-{d}T00:00:00+05:30" for d in range(23, 31)]
    assert body["readings"][0]["consumption_kwh"] is None
    summary = body["summary"]
    assert (summary["count"], summary["interval_minutes"], summary["missing_intervals"]) == (8, 1440, 0)
    assert summary["consumption_kwh"] == round(rows[-1].register_kwh - rows[-8].register_kwh, 2)
    assert body["freshness"]["stale"] is False


async def test_readings_for_an_explicit_window(api):
    day = await api.get("/v1/meters/J100000/readings", {"from": "2026-06-02", "to": "2026-06-02"})
    assert len(day["readings"]) == 48
    assert (day["readings"][0]["timestamp"], day["readings"][-1]["timestamp"]) == (
        "2026-06-02T00:00:00+05:30",
        "2026-06-02T23:30:00+05:30",
    )
    assert day["summary"]["interval_minutes"] == 30

    hours = await api.get("/v1/meters/J100000/readings", {"from": "2026-06-02T10:00", "to": "2026-06-02T12:00+05:30"})
    assert [r["timestamp"][11:16] for r in hours["readings"]] == ["10:00", "10:30", "11:00", "11:30", "12:00"]


@pytest.mark.parametrize(
    "params",
    [{"from": "2026-06-05", "to": "2026-06-01"}, {"from": "01/06/2026"}],
    ids=["inverted", "portal-date-format"],
)
async def test_invalid_readings_windows(api, params):
    assert (await api.problem("/v1/meters/J100000/readings", 422, params))["code"] == "validation_error"


async def test_the_blank_duplicate_row_is_merged(api):
    body = await api.get("/v1/meters/J100089/readings")
    last = body["readings"][-1]
    assert (last["timestamp"], last["register_kwh"], last["voltage_v"]) == ("2026-06-30T00:00:00+05:30", 12732.34, 229)
    assert last["flags"] == ["duplicate"]
    assert body["summary"]["flags"] == {"duplicate": 1}


async def test_daily_consumption_stops_where_the_data_does(api, stored):
    registers = {r.timestamp.day: r.register_kwh for r in stored("J100040")}
    body = await api.get("/v1/meters/J100040/consumption", {"from": "2026-06-27", "to": "2026-06-30"})
    buckets = body["buckets"]
    # 30 June would need a reading on 1 July: the data ends at 30 June 00:00, and so do the buckets.
    assert [b["start"] for b in buckets] == [f"2026-06-{d}T00:00:00+05:30" for d in (27, 28, 29)]
    assert (body["start"], body["end"]) == ("2026-06-27T00:00:00+05:30", "2026-06-30T00:00:00+05:30")
    for day, bucket in zip((27, 28, 29), buckets, strict=True):
        assert bucket["complete"] and bucket["coverage"] == 1
        assert bucket["consumption_kwh"] == round(registers[day + 1] - registers[day], 2)
        assert 0.92 < bucket["power_factor"] < 0.93  # the portal's kVAh is kWh x 1.08
    assert body["total_kwh"] == pytest.approx(registers[30] - registers[27], abs=0.01)


@pytest.mark.parametrize(
    ("params", "days"),
    [
        ({"from": "2000-01-01", "to": "2099-12-31"}, 29),  # a century asks for the data's 29 days, no more
        ({"from": "2026-07-10", "to": "2026-07-12"}, 0),  # after the data
        ({"from": "2026-06-30"}, 0),  # the last reading closes no day
    ],
    ids=["century", "after-the-data", "last-reading"],
)
async def test_consumption_windows_are_clamped_to_the_cached_data(api, params, days):
    body = await api.get("/v1/meters/J100040/consumption", params)
    assert len(body["buckets"]) == days
    if not days:
        assert (body["start"], body["end"], body["total_kwh"]) == (None, None, None)


async def test_consumption_total_is_the_register_difference_across_a_gap(api, stored):
    await api.get("/v1/meters/J100040/readings")  # cache the series...
    registers = {r.timestamp.day: r.register_kwh for r in stored("J100040")}
    store = api.services.store
    kept = [  # ...then lose the 28 June reading from it
        RawReading(datetime.fromisoformat(r["ts"]), r["kwh"], r["kvah"], r["voltage"])
        for r in store.get_readings("J100040")
        if not r["ts"].startswith("2026-06-28")
    ]
    store.replace_readings("J100040", kept, {}, api.services.now(), 1440)

    body = await api.get("/v1/meters/J100040/consumption", {"from": "2026-06-27", "to": "2026-06-29"})
    assert [b["consumption_kwh"] for b in body["buckets"]] == [
        None,  # 27 -> 29 June spans a bucket boundary: no single day can claim it
        None,
        round(registers[30] - registers[29], 2),
    ]
    # The register difference loses nothing to the gap.
    assert body["total_kwh"] == pytest.approx(registers[30] - registers[27], abs=0.01)


async def test_half_hourly_consumption(api):
    body = await api.get("/v1/meters/J100000/consumption", {"from": "2026-06-04", "to": "2026-06-05"})
    day4, day5 = body["buckets"]
    assert day4["complete"] and day4["coverage"] == 1
    assert not day5["complete"] and day5["coverage"] == round(47 / 48, 4)  # 05/06 23:30 is the last reading

    hourly = await api.get(
        "/v1/meters/J100000/consumption", {"from": "2026-06-02T10:00", "to": "2026-06-02T13:00", "granularity": "hour"}
    )
    assert [b["start"][11:16] for b in hourly["buckets"]] == ["10:00", "11:00", "12:00"]
    assert all(b["complete"] and b["power_factor"] is None for b in hourly["buckets"])


async def test_hourly_consumption_needs_sub_hourly_readings(api):
    body = await api.problem("/v1/meters/J100040/consumption", 422, {"granularity": "hour"})
    assert body["code"] == "granularity_unavailable"


async def test_cached_readings_are_served_stale_when_the_portal_fails(start_api):
    api = await start_api(readings_ttl_s=0, portal_max_wait_s=0.2)  # always refresh; give up fast
    fresh = await api.get("/v1/meters/J100040/readings")
    assert fresh["freshness"]["stale"] is False

    api.portal.fail_next = [500] * 10
    stale = await api.get("/v1/meters/J100040/readings")
    assert stale["freshness"] == {"fetched_at": fresh["freshness"]["fetched_at"], "stale": True}
    assert stale["readings"] == fresh["readings"]

    body = await api.problem("/v1/meters/J100004/readings", 503)  # nothing cached to fall back on
    assert body["code"] == "upstream_unavailable"
    api.portal.fail_next.clear()


# ----------------------------------------------------------------------------- transformers & network


async def test_transformers(api, export_records):
    page = await api.get("/v1/transformers")
    by_code = {t["code"]: t for t in page["items"]}
    meters_per_dt = Counter(r["dtCode"] for r in export_records)
    assert {code: by_code[code]["meter_count"] for code in meters_per_dt} == meters_per_dt
    dt7 = by_code["DT-007"]
    assert (dt7["name"], dt7["feeder_code"], dt7["capacity_kva"]) == ("Sanganer DT 7", "F-007", 63)
    assert dt7["name_variants"] == ["Old Malviya Nagar Xfmr"]
    assert dt7["meters_by_status"] == {"decommissioned": 1, "installed": 3}
    assert all("location" not in t for t in page["items"])  # meter GPS says nothing about DT sites
    assert [t["code"] for t in (await api.get("/v1/transformers", {"feeder": "f-007"}))["items"]] == ["DT-007"]


async def test_transformer_detail(api):
    dt = await api.get("/v1/transformers/dt-007")
    assert dt["code"] == "DT-007"
    assert [m["meter_id"] for m in dt["meters"]] == ["J100006", "J100400", "J100401", "J100402"]
    assert dt["network"]["feeder"] == {"code": "F-007", "name": "Feeder 7"}
    assert "location" not in dt


async def test_network_overview(api):
    body = await api.get("/v1/network")
    assert body["is_tree"] is False
    assert body["node_counts"] == {
        "zone": 3,
        "circle": 6,
        "division": 8,
        "subdivision": 9,
        "substation": 9,
        "feeder": 11,
        "transformer": 11,
    }
    assert {e["child_level"]: e["functional"] for e in body["edges"]} == {
        "circle": True,
        "division": False,
        "subdivision": False,
        "substation": False,
        "feeder": True,
        "transformer": True,
    }


async def test_network_tree_counts_add_up(api):
    tree = await api.get("/v1/network/tree")
    assert sum(zone["meter_count"] for zone in tree) == 20
    for node in walk(tree):
        if node["children"]:
            assert node["meter_count"] == sum(c["meter_count"] for c in node["children"])
            assert node["transformer_count"] == sum(c["transformer_count"] for c in node["children"])
    d01 = [n for n in walk(tree) if (n["level"], n["code"]) == ("division", "D-01")]
    assert sorted(n["meter_count"] for n in d01) == [1, 1, 3]  # once under each of three circles
    assert all(zone["children"] == [] for zone in await api.get("/v1/network/tree", {"depth": 1}))


async def test_network_level_and_node(api):
    divisions = {n["code"]: n for n in await api.get("/v1/network/division")}
    assert divisions["D-01"]["parent_codes"] == ["C-01", "C-03", "C-05"]
    assert (divisions["D-01"]["meter_count"], divisions["D-01"]["transformer_count"]) == (5, 3)

    d01 = await api.get("/v1/network/division/d-01")
    assert [(p["code"], p["transformer_count"], p["meter_count"]) for p in d01["parents"]] == [
        ("C-01", 1, 3),
        ("C-03", 1, 1),
        ("C-05", 1, 1),
    ]
    assert [c["code"] for c in d01["children"]] == ["SD-01", "SD-07", "SD-11"]
    assert d01["transformers"] == ["DT-001", "DT-011", "DT-021"]
    assert d01["meters_by_status"] == {"decommissioned": 2, "faulty": 2, "installed": 1}

    dt7 = await api.get("/v1/network/transformer/DT-007")
    assert dt7["name_variants"] == ["Old Malviya Nagar Xfmr"]
    assert [p["code"] for p in dt7["parents"]] == ["F-007"] and dt7["children"] == []
    await api.problem("/v1/network/county", 422)


async def test_data_quality(warm_api):
    report = await warm_api.get("/v1/data-quality")
    assert report["meters_with_issues"] == 7
    assert report["issues_by_code"] == {"blank_code": 2, "blank_name": 2, "stale_name": 3}
    assert [m["meter_id"] for m in report["meters"]] == [
        "J100011",
        "J100153",
        "J100162",
        "J100218",
        "J100400",
        "J100401",
        "J100402",
    ]
    assert report["readings"] == {
        "meters_cached": 20,
        "duplicate_timestamps": 1,
        "conflicting_duplicates": 0,
        "missing_values": 0,
        "gaps": 0,
        "register_decrease": 0,
    }
    assert report["network"]["is_tree"] is False


# ----------------------------------------------------------------------------- insights


async def test_anomaly_report(warm_api):
    freeze(warm_api, AFTER_THE_DATA)
    report = await warm_api.get("/v1/insights/anomalies")
    assert (report["meters_analysed"], report["meters_total"]) == (20, 20)
    # The fixture data ends in June 2026, so by October every meter is stale.
    assert report["meters_by_rule"] == {"decommissioned_reporting": 4, "duplicate_timestamps": 1, "stale": 20}
    # Meters that are merely stale are counted, but listed only when asked for.
    assert [m["meter_id"] for m in report["meters"]] == ["J100000", "J100006", "J100011", "J100089", "J100100"]
    stale = await warm_api.get("/v1/insights/anomalies", {"rule": "stale"})
    assert len(stale["meters"]) == 20

    errors = await warm_api.get("/v1/insights/anomalies", {"severity": "error"})
    assert errors["meters_by_rule"] == {"decommissioned_reporting": 4}
    assert [m["meter_id"] for m in errors["meters"]] == ["J100000", "J100006", "J100011", "J100100"]

    duplicates = await warm_api.get("/v1/insights/anomalies", {"rule": "duplicate_timestamps"})
    [meter] = duplicates["meters"]
    assert meter["meter_id"] == "J100089"
    assert [a["rule"] for a in meter["anomalies"]] == ["stale", "duplicate_timestamps"]  # all rules, for context


async def test_meter_anomalies(api):
    freeze(api, AFTER_THE_DATA)
    body = await api.get("/v1/meters/j100000/anomalies")
    assert (body["meter_id"], body["status"], body["transformer_code"]) == ("J100000", "decommissioned", "DT-001")
    assert [a["rule"] for a in body["anomalies"]] == ["decommissioned_reporting", "stale"]  # errors first


async def test_consumption_insight(warm_api, energy, export_records):
    def register(meter_id: str, day: int) -> float:
        return float(next(r["kwh"] for r in energy[meter_id] if r["timestamp"] == f"{day:02d}/06/2026 00:00"))

    used = {r["meterId"]: register(r["meterId"], 5) - register(r["meterId"], 1) for r in export_records}
    status = {r["meterId"]: r["installStatus"].lower() for r in export_records}
    window = {"from": "2026-06-01", "to": "2026-06-04"}  # four complete days for every meter

    body = await warm_api.get("/v1/insights/consumption", {**window, "group_by": "status"})
    expected = {s: sum(kwh for m, kwh in used.items() if status[m] == s) for s in set(status.values())}
    assert {g["key"]: g["consumption_kwh"] for g in body["groups"]} == pytest.approx(expected, abs=0.01)
    assert {g["key"]: g["meter_count"] for g in body["groups"]} == Counter(status.values())
    assert body["total_kwh"] == pytest.approx(sum(used.values()), abs=0.01)
    assert sum(g["share"] for g in body["groups"]) == pytest.approx(1, abs=1e-3)
    assert (body["meters_analysed"], body["meters_total"]) == (20, 20)

    active = await warm_api.get(
        "/v1/insights/consumption", {**window, "group_by": "status", "include_decommissioned": "false"}
    )
    assert {g["key"] for g in active["groups"]} == {"installed", "faulty"}
    assert active["meters_analysed"] == 16

    by_dt = await warm_api.get("/v1/insights/consumption", {**window, "group_by": "transformer"})
    dt7 = next(g for g in by_dt["groups"] if g["key"] == "DT-007")
    assert (dt7["name"], dt7["meter_count"], dt7["meters_with_data_count"]) == ("Sanganer DT 7", 4, 4)
    assert dt7["consumption_kwh"] == pytest.approx(
        sum(used[m] for m in ("J100006", "J100400", "J100401", "J100402")), abs=0.01
    )


async def test_consumption_insight_states_which_days_it_counted(warm_api):
    window = await warm_api.get("/v1/insights/consumption", {"from": "2026-06-01", "to": "2026-06-04"})
    assert (window["days_counted"], window["first_day"], window["last_day"]) == (4, "2026-06-01", "2026-06-04")
    assert (window["start"], window["end"]) == ("2026-06-01T00:00:00+05:30", "2026-06-05T00:00:00+05:30")
    # By default: the 7 days up to the latest data (30 June 00:00, which closes 29 June).
    latest = await warm_api.get("/v1/insights/consumption")
    assert (latest["days_counted"], latest["first_day"], latest["last_day"]) == (7, "2026-06-23", "2026-06-29")
    empty = await warm_api.get("/v1/insights/consumption", {"from": "2026-07-10", "to": "2026-07-12"})
    assert (empty["days_counted"], empty["first_day"], empty["last_day"], empty["total_kwh"]) == (0, None, None, 0)


async def test_consumption_insight_leaves_out_a_day_without_its_closing_reading(api):
    # Only J100000 is cached: its readings stop at 05/06 23:30, so 5 June is never complete.
    await api.get("/v1/meters/J100000/readings")
    june_4 = await api.get("/v1/meters/J100000/consumption", {"from": "2026-06-04", "to": "2026-06-04"})
    body = await api.get("/v1/insights/consumption", {"from": "2026-06-04", "to": "2026-06-05"})
    assert (body["days_counted"], body["first_day"], body["last_day"]) == (1, "2026-06-04", "2026-06-04")
    assert body["total_kwh"] == pytest.approx(june_4["buckets"][0]["consumption_kwh"], abs=0.01)
    assert (body["meters_analysed"], body["meters_total"]) == (1, 20)


async def test_anomaly_counts_without_the_meter_list(warm_api):
    freeze(warm_api, datetime(2026, 6, 30, 12, tzinfo=IST))  # half a day after the daily meters' last reading
    full = await warm_api.get("/v1/insights/anomalies")
    counts = await warm_api.get("/v1/insights/anomalies", {"include_meters": "false"})
    assert counts["meters"] == []
    assert counts["meters_by_rule"] == full["meters_by_rule"]
    # Only the half-hourly meters are stale by then: their data ends on 5 June.
    assert full["meters_by_rule"] == {"decommissioned_reporting": 4, "duplicate_timestamps": 1, "stale": 7}
    assert (counts["meters_analysed"], counts["meters_total"]) == (20, 20)
    stale = await warm_api.get("/v1/insights/anomalies", {"rule": "stale"})
    assert [m["meter_id"] for m in stale["meters"]] == HALF_HOURLY


# ----------------------------------------------------------------------------- API key


async def test_api_key(start_api):
    api = await start_api(api_key="s3cret")
    body = await api.problem("/v1/meters", 401)
    assert body["code"] == "unauthorized"
    await api.problem("/v1/meters", 401, headers={"X-API-Key": "wrong"})
    assert (await api.http.get("/v1/meters", headers={"X-API-Key": "s3cret"})).status_code == 200
    assert (await api.http.get("/healthz")).status_code == 200
    assert (await api.http.post("/v1/sync")).status_code == 401
    assert (await api.http.post("/v1/sync", headers={"X-API-Key": "s3cret"})).status_code == 200


async def test_status_needs_the_api_key_but_health_and_docs_do_not(start_api):
    api = await start_api(api_key="s3cret")
    assert (await api.problem("/v1/status", 401))["code"] == "unauthorized"
    assert (await api.http.get("/v1/status", headers={"X-API-Key": "s3cret"})).status_code == 200
    for path in ("/healthz", "/openapi.json", "/docs"):
        assert (await api.http.get(path)).status_code == 200, path


# ----------------------------------------------------------------------------- the query contract


async def test_unknown_query_parameters_are_rejected(api):
    body = await api.problem("/v1/meters", 422, {"transformer_code": "DT-007"})
    assert body["code"] == "validation_error"
    assert body["errors"] == [
        {
            "location": ["query", "transformer_code"],
            "message": "unknown query parameter; did you mean 'transformer'?",
            "type": "unknown_parameter",
        }
    ]
    for path in ("/v1/insights/anomalies", "/v1/network", "/v1/status"):
        await api.problem(path, 422, {"rules": "stale"})


@pytest.mark.parametrize(
    ("alias", "key"),
    [("data_issue_count", "issues"), ("transformer_code", "transformer"), ("reading_interval_minutes", "interval")],
)
async def test_response_field_names_work_as_sort_keys(warm_api, alias, key):
    for order in ("", "-"):
        by_alias = await warm_api.get("/v1/meters", {"sort": order + alias})
        assert ids(by_alias) == ids(await warm_api.get("/v1/meters", {"sort": order + key}))


@pytest.mark.parametrize(
    "params",
    [
        {"from": "1999-12-31"},
        {"to": "2101-01-01"},
        {"to": "9999-12-31"},
        {"from": "20260602"},
        {"from": "2026-W23-2"},
        {"from": "2026-06-02 10:00"},
    ],
    ids=["before-2000", "after-2100", "year-9999", "compact-date", "week-date", "no-T"],
)
async def test_dates_outside_the_documented_forms_are_rejected(api, params):
    for path in ("/v1/meters/J100040/readings", "/v1/meters/J100040/consumption", "/v1/insights/consumption"):
        assert (await api.problem(path, 422, params))["code"] == "validation_error", path


async def test_a_bad_window_is_rejected_before_the_portal_is_asked(api):
    for params in ({"from": "bad"}, {"from": "2026-06-05", "to": "2026-06-01"}):
        for endpoint in ("readings", "consumption"):
            await api.problem(f"/v1/meters/J100000/{endpoint}", 422, params)
    assert api.portal.calls["/portal/meters/J100000/energy"] == 0


async def test_windows_in_other_offsets_are_read_in_ist(api):
    ist = await api.get("/v1/meters/J100040/consumption", {"from": "2026-06-01", "to": "2026-06-03"})
    utc = await api.get(
        "/v1/meters/J100040/consumption", {"from": "2026-05-31T18:30:00Z", "to": "2026-06-03T18:29:59+00:00"}
    )
    assert utc["buckets"] == ist["buckets"] and len(ist["buckets"]) == 3
    assert utc["start"] == "2026-06-01T00:00:00+05:30"

    readings = await api.get("/v1/meters/J100000/readings", {"from": "2026-06-02T04:30:00Z", "to": "2026-06-02T05:30Z"})
    assert [r["timestamp"] for r in readings["readings"]] == [
        "2026-06-02T10:00:00+05:30",
        "2026-06-02T10:30:00+05:30",
        "2026-06-02T11:00:00+05:30",
    ]
    assert (readings["start"], readings["end"]) == ("2026-06-02T10:00:00+05:30", "2026-06-02T11:00:00+05:30")


async def test_a_bare_to_date_means_the_seven_whole_days_ending_that_day(warm_api):
    days = [f"2026-06-{d}" for d in range(14, 21)]
    readings = await warm_api.get("/v1/meters/J100040/readings", {"to": "2026-06-20"})
    assert [r["timestamp"][:10] for r in readings["readings"]] == days
    consumption = await warm_api.get("/v1/meters/J100040/consumption", {"to": "2026-06-20"})
    assert [b["start"][:10] for b in consumption["buckets"]] == days
    insight = await warm_api.get("/v1/insights/consumption", {"to": "2026-06-20"})
    assert (insight["days_counted"], insight["first_day"], insight["last_day"]) == (7, days[0], days[-1])


async def test_a_window_after_the_data_is_empty_not_inverted(api):
    body = await api.get("/v1/meters/J100040/readings", {"from": "2026-07-10"})
    assert body["readings"] == []
    assert body["start"] == body["end"] == "2026-07-10T00:00:00+05:30"
    assert body["last_reading_at"] == "2026-06-30T00:00:00+05:30"


async def test_an_unknown_anomaly_rule_is_rejected(api):
    assert (await api.problem("/v1/insights/anomalies", 422, {"rule": "no_such_rule"}))["code"] == "validation_error"


async def test_consumption_insight_with_nothing_cached(api):
    body = await api.get("/v1/insights/consumption")
    assert (body["start"], body["end"], body["total_kwh"], body["meters_analysed"]) == (None, None, None, 0)
    assert (body["groups"], body["groups_total"]) == ([], 0)

    asked = await api.get("/v1/insights/consumption", {"from": "2026-06-01", "to": "2026-06-04", "group_by": "status"})
    assert (asked["total_kwh"], asked["meters_analysed"]) == (None, 0)  # nothing analysed is not "0 kWh"
    assert sum(g["meter_count"] for g in asked["groups"]) == 20
    assert all(g["meters_with_data_count"] == 0 for g in asked["groups"])


async def test_consumption_insight_says_how_many_groups_there_are(warm_api):
    body = await warm_api.get("/v1/insights/consumption", {"group_by": "meter", "limit": 3})
    assert (len(body["groups"]), body["groups_total"]) == (3, 20)
    kwh = [g["consumption_kwh"] for g in body["groups"]]
    assert kwh == sorted(kwh, reverse=True)  # the top consumers


# ----------------------------------------------------------------------------- error mapping


async def test_a_meter_the_portal_no_longer_has_is_not_found(api):
    api.portal.remove_meter("J100040")  # still in our index, gone from the portal
    body = await api.problem("/v1/meters/J100040/readings", 404)
    assert body["code"] == "meter_not_found"


async def test_an_unexpected_error_is_a_problem_response(api, monkeypatch):
    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("an internal detail")

    monkeypatch.setattr(api.services.store, "search_meters", broken)
    transport = httpx.ASGITransport(app=api.app, raise_app_exceptions=False)  # what a real server sends
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as http:
        resp = await http.get("/v1/meters")
    assert (resp.status_code, resp.headers["content-type"]) == (500, PROBLEM_JSON)
    assert resp.json()["code"] == "internal_error"
    assert "an internal detail" not in resp.text


async def test_our_own_queue_backing_up_is_busy_not_rate_limited(start_api):
    api = await start_api(portal_max_wait_s=0.2)
    throttle = TokenBucket(1 / 60, 1)  # one token a minute...
    assert throttle.try_acquire()  # ...and it is already spent
    api.services.client._throttle = throttle
    resp = await api.http.get("/v1/meters/J100000/readings")  # nothing cached: it needs the portal
    assert (resp.status_code, resp.json()["code"]) == (503, "busy")
    assert int(resp.headers["retry-after"]) >= 1
    assert api.services.client.stats.rate_limited == 0  # the portal never said 429


async def test_a_non_ascii_api_key_is_refused_not_a_crash(start_api):
    api = await start_api(api_key="s3cret")
    resp = await api.http.get("/v1/meters", headers={"X-API-Key": "été".encode()})
    assert (resp.status_code, resp.json()["code"]) == (401, "unauthorized")

    resp = await api.http.get("/v1/meters", headers={"X-API-Key": "été".encode("latin-1")})  # what a browser sends
    assert resp.status_code == 401


# ----------------------------------------------------------------------------- edges found in review


@pytest.mark.parametrize(
    "params",
    [{"to": "9999-12-31T23:59:59Z"}, {"from": "0001-01-01T00:00:00+05:30"}, {"to": "9999-12-31T23:00:00-05:00"}],
    ids=["year-9999-utc", "year-1-ist", "year-9999-west"],
)
async def test_extreme_instants_with_an_offset_are_rejected_not_a_crash(api, params):
    for path in ("/v1/meters/J100040/readings", "/v1/meters/J100040/consumption", "/v1/insights/consumption"):
        body = await api.problem(path, 422, params)
        assert "between 2000-01-01 and 2100-12-31" in body["detail"], path


async def test_an_unencoded_plus_in_an_offset_is_understood(api):
    # Typed straight into a URL, "+05:30" reaches the server as " 05:30".
    for offset in ("+05:30", "+0530", "+05:30:00"):
        url = f"/v1/meters/J100000/readings?from=2026-06-02T10:00:00{offset}&to=2026-06-02T11:00:00{offset}"
        resp = await api.http.get(url)
        assert resp.status_code == 200, resp.text
        assert [r["timestamp"][11:16] for r in resp.json()["readings"]] == ["10:00", "10:30", "11:00"]
    resp = await api.http.get("/v1/meters/J100000/readings?from=2026-06-02T10:00:00+05&to=2026-06-02T10:30:00+05")
    assert [r["timestamp"][11:16] for r in resp.json()["readings"]] == ["10:30", "11:00"]  # 10:00+05 is 10:30 IST


@pytest.mark.parametrize("to", ["2026-06-20T00:00:00+05:30", "2026-06-19T18:30:00Z"], ids=["ist", "utc"])
async def test_a_to_exactly_at_midnight_closes_the_day_before_on_both_endpoints(warm_api, to):
    params = {"from": "2026-06-14", "to": to}
    consumption = await warm_api.get("/v1/meters/J100040/consumption", params)
    insight = await warm_api.get("/v1/insights/consumption", params)
    assert [b["start"][:10] for b in consumption["buckets"]] == [f"2026-06-{d}" for d in range(14, 20)]
    assert (insight["days_counted"], insight["first_day"], insight["last_day"]) == (6, "2026-06-14", "2026-06-19")
    assert consumption["end"] == insight["end"] == "2026-06-20T00:00:00+05:30"


async def test_a_register_creeping_backwards_reads_the_same_on_every_endpoint(api):
    for i, row in enumerate(api.portal.energy["J100001"]):  # 0.02 down per reading: each within rounding slack
        row["kwh"], row["kvah"] = f"{5000 - 0.02 * i:.2f}", f"{5400 - 0.02 * i:.2f}"
    window = {"from": "2026-06-01", "to": "2026-06-05"}
    readings = await api.get("/v1/meters/J100001/readings", window)
    consumption = await api.get("/v1/meters/J100001/consumption", window)
    anomalies = {a["rule"]: a for a in (await api.get("/v1/meters/J100001/anomalies"))["anomalies"]}
    flagged = readings["summary"]["flags"]["register_decrease"]
    assert flagged == anomalies["register_decrease"]["occurrences"] > 100
    assert consumption["total_kwh"] is None  # "the register went down", not "0 kWh used"
    assert readings["summary"]["consumption_kwh"] is None
    assert not any(b["complete"] for b in consumption["buckets"])

    # A window that starts mid-series flags the same readings as the whole series does.
    def decreases(body: dict) -> list[str]:
        return [r["timestamp"] for r in body["readings"] if "register_decrease" in r["flags"]]

    part = await api.get("/v1/meters/J100001/readings", {"from": "2026-06-03T00:30", "to": "2026-06-03T03:00"})
    assert decreases(part) == [t for t in decreases(readings) if "2026-06-03T00:30" <= t[:16] <= "2026-06-03T03:00"]
    assert len(decreases(part)) == 3


async def test_a_wrong_method_says_which_one_is_allowed(api):
    resp = await api.http.post("/v1/meters")
    assert (resp.status_code, resp.json()["code"], resp.headers["allow"]) == (405, "method_not_allowed", "GET")
    assert resp.headers["content-type"] == PROBLEM_JSON
    assert (await api.http.get("/v1/sync")).headers["allow"] == "POST"
    static = await api.http.post("/app/")  # the static mount's own 405 carries no Allow header
    assert (static.status_code, static.headers["allow"]) == (405, "GET, HEAD")


def test_a_blank_api_key_means_no_key_and_an_unsendable_one_is_refused():
    base = {"portal_email": EMAIL, "portal_password": PASSWORD}
    assert Settings(_env_file=None, api_key="   ", **base).api_key is None
    assert Settings(_env_file=None, api_key="s3cret", **base).api_key is not None
    for unusable in ("clé", "two words", "smart’quote", "Sup3r-S3cret\n"):  # none survives a browser's header
        with pytest.raises(ValidationError, match="printable ASCII") as caught:
            Settings(_env_file=None, api_key=unusable, **base)
        assert unusable.strip() not in str(caught.value)  # the start-up error must not print the key
