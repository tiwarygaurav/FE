"""Reference sync (export, crawl fallback) and the readings cache, against the FakePortal."""

from __future__ import annotations

import asyncio
import copy
import json
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta

import pytest

from urja_api import sync as sync_module
from urja_api.derived import DerivedViews
from urja_api.portal.errors import PortalProtocolError, PortalUnavailable, RequestQueueFull
from urja_api.store import Store
from urja_api.sync import SyncRejected, SyncService


@pytest.fixture
def make_store(tmp_path) -> Iterator[Callable[[str], Store]]:
    stores: list[Store] = []

    def make(name: str = "urja") -> Store:
        stores.append(Store(tmp_path / f"{name}.sqlite3"))
        return stores[-1]

    yield make
    for store in stores:
        store.close()


async def test_reference_sync_from_the_export(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    # The fixture's DT list has 40 transformers but its 20 meters hang off 11 of them: a
    # transformer's place in the network is only known from its meters, so 11 are served.
    assert await sync.sync_reference_data() == {
        "source": "export",
        "meters": 20,
        "transformers": 11,
        "warnings": ["29 transformers on the portal's list have no meters and are not served"],
    }
    assert len(store.list_transformers()) == 11
    assert len(store.meter_ids()) == 20
    assert store.get_meter("J100000")["latitude"] == 26.938961
    assert sync.reference_status()["source"] == "export"


async def test_crawl_fallback_rebuilds_the_same_network(make_portal, connect, make_store):
    exported, crawled = make_store("export"), make_store("crawl")
    await SyncService(connect(make_portal()), exported).sync_reference_data()

    broken = make_portal(signature_tolerance_s=-1)  # every export signature is rejected
    summary = await SyncService(connect(broken), crawled).sync_reference_data()
    assert (summary["source"], summary["meters"]) == ("crawl", 20)
    assert summary["warnings"][0].startswith("export unusable, used detail-page crawl")
    assert "20 meters have no location (detail pages carry none)" in summary["warnings"]
    assert crawled.transformer_paths() == exported.transformer_paths()
    assert all(row["latitude"] is None for row in crawled.all_meters().values())  # nothing to carry over

    # The detail page loses the code of a level with a blank name; the transformer supplies it.
    issues = {i["code"]: i for i in json.loads(crawled.get_meter("J100162")["issues_json"])}
    assert (issues["blank_code"]["level"], issues["blank_code"]["resolved"]) == ("feeder", "F-003")
    assert issues["blank_name"]["resolved"] == "Feeder 3"


async def test_a_failed_sync_keeps_serving_the_last_snapshot(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal, max_wait_s=0.1), store)
    await sync.sync_reference_data()
    fake_portal.fail_next = [503] * 10
    with pytest.raises(PortalUnavailable):
        await sync.sync_reference_data()
    fake_portal.fail_next.clear()
    assert len(store.meter_ids()) == 20
    status = sync.reference_status()
    assert status["last_run"]["status"] == "failed"
    assert status["source"] == "export"  # from the last successful run


async def test_concurrent_readings_requests_share_one_portal_call(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    await sync.sync_reference_data()
    results = await asyncio.gather(*(sync.ensure_readings("J100040") for _ in range(5)))
    assert fake_portal.calls["/portal/meters/J100040/energy"] == 1
    assert len(set(results)) == 1
    assert len(store.get_readings("J100040")) == 30

    await sync.ensure_readings("J100040")  # within the TTL: served from the cache
    assert fake_portal.calls["/portal/meters/J100040/energy"] == 1
    await sync.ensure_readings("J100040", force=True)
    assert fake_portal.calls["/portal/meters/J100040/energy"] == 2


async def test_warm_up_fills_the_readings_cache(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal, rate_limit_per_minute=60_000), store)
    await sync.sync_reference_data()
    await sync.warm_all_readings()
    assert {k: sync.warmup[k] for k in ("state", "done", "total", "failed")} == {
        "state": "done",
        "done": 20,
        "total": 20,
        "failed": 0,
    }
    assert store.readings_fetch_summary()["meters"] == 20
    duplicates = [r["ts"] for r in store.get_readings("J100089") if r["duplicate"]]
    assert duplicates == ["2026-06-30T00:00:00+05:30"]


async def test_a_sync_that_would_halve_the_index_is_rejected(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal, rate_limit_per_minute=60_000), store)
    await sync.sync_reference_data()
    exported = fake_portal.meters

    fake_portal.meters = exported[:9]  # under half of the 20 indexed: far more likely a portal fault
    with pytest.raises(SyncRejected, match=r"portal returned 9 meters \(index has 20\)"):
        await sync.sync_reference_data()
    assert len(store.meter_ids()) == 20  # the previous snapshot is still served
    status = sync.reference_status()
    assert (status["last_run"]["status"], status["source"]) == ("failed", "export")
    assert status["last_run"]["error"].startswith("SyncRejected: ")

    fake_portal.meters = exported[:10]  # exactly half is accepted
    assert (await sync.sync_reference_data())["meters"] == 10
    assert len(store.meter_ids()) == 10


async def test_an_empty_export_is_rejected_even_into_an_empty_index(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    fake_portal.meters = []
    with pytest.raises(SyncRejected, match="portal returned 0 meters"):
        await sync.sync_reference_data()
    assert not store.has_reference_data()
    assert store.last_sync_runs(1)[0]["status"] == "failed"


async def test_warm_up_waits_for_the_budget_before_each_meter_and_counts_failures(
    fake_portal, connect, make_store, monkeypatch
):
    store = make_store()
    client = connect(fake_portal, rate_limit_per_minute=60_000)
    sync = SyncService(client, store)
    await sync.sync_reference_data()
    energy_calls_at_each_wait: list[int] = []

    async def wait_for_budget() -> None:
        energy_calls_at_each_wait.append(sum(n for path, n in fake_portal.calls.items() if path.endswith("/energy")))

    monkeypatch.setattr(client, "wait_for_budget", wait_for_budget)
    fake_portal.fail_next = [200]  # the first meter's series comes back as non-JSON
    fake_portal.remove_meter("J100400")  # and the portal no longer knows this meter
    await sync.warm_all_readings()

    assert energy_calls_at_each_wait == list(range(20))  # one wait before each meter's fetch
    assert {k: sync.warmup[k] for k in ("state", "done", "total", "failed")} == {
        "state": "done",
        "done": 20,
        "total": 20,
        "failed": 2,
    }
    assert sync.warmup["errors"] == {"PortalNotFound": 1, "PortalProtocolError": 1}
    assert store.readings_fetch_summary()["meters"] == 18


async def test_the_detected_reading_interval_is_stored(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    await sync.sync_reference_data()
    fake_portal.energy["J100098"] = []  # a meter the portal has no readings for
    for meter_id in ("J100000", "J100040", "J100098"):
        await sync.ensure_readings(meter_id)

    info = {meter_id: store.readings_fetch_info(meter_id) for meter_id in ("J100000", "J100040", "J100098")}
    assert {m: row["interval_minutes"] for m, row in info.items()} == {"J100000": 30, "J100040": 1440, "J100098": None}
    assert (info["J100000"]["reading_count"], info["J100000"]["first_ts"], info["J100000"]["last_ts"]) == (
        240,
        "2026-06-01T00:00:00+05:30",
        "2026-06-05T23:30:00+05:30",
    )
    assert (info["J100098"]["reading_count"], info["J100098"]["first_ts"]) == (0, None)
    # The meter row carries it too; it stays null until a meter's readings are fetched.
    assert store.get_meter("J100000")["interval_minutes"] == 30
    assert store.get_meter("J100001")["interval_minutes"] is None


async def test_parsed_series_and_daily_buckets_are_memoised_per_readings_version(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    derived = DerivedViews(store)
    await sync.sync_reference_data()
    await sync.ensure_readings("J100040")
    series, daily = derived.series(), derived.daily()
    assert derived.series() is series and derived.daily() is daily  # nothing changed: computed once
    assert len(daily["J100040"]) == 29  # 1-29 June; nothing closes 30 June

    version = store.readings_version
    await sync.ensure_readings("J100000")
    assert store.readings_version != version
    assert derived.series() is not series and derived.daily() is not daily
    assert set(derived.series()) == set(derived.daily()) == {"J100000", "J100040"}

    fake_portal.energy["J100040"][-1]["kwh"] = "99999.99"  # the portal's data changes
    await sync.ensure_readings("J100040", force=True)
    assert derived.series()["J100040"][-1].register_kwh == 99999.99


async def test_anomaly_results_are_recomputed_when_the_reference_data_changes(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    derived = DerivedViews(store)
    await sync.sync_reference_data()
    await sync.ensure_readings("J100000")  # decommissioned, yet still reporting
    assert "decommissioned_reporting" in {a.rule for a in derived.rules()["J100000"].anomalies}

    next(m for m in fake_portal.meters if m["meterId"] == "J100000")["installStatus"] = "Installed"
    await sync.sync_reference_data()  # same readings, new status: the memo must not be reused
    assert "decommissioned_reporting" not in {a.rule for a in derived.rules()["J100000"].anomalies}


async def test_refetching_an_unchanged_series_keeps_the_memoised_views(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    derived = DerivedViews(store)
    await sync.sync_reference_data()
    await sync.ensure_readings("J100040")
    series, version = derived.series(), store.readings_version
    fetched_at, _ = await sync.ensure_readings("J100040", force=True)  # the portal sends the same payload
    assert store.readings_version == version
    assert derived.series() is series
    assert store.readings_fetch_info("J100040")["fetched_at"] == fetched_at.isoformat()  # still recorded as fresh


# ----------------------------------------------------------------------------- robustness


async def test_a_failed_refresh_serves_the_cache_at_once_and_is_remembered(
    fake_portal, connect, make_store, monkeypatch
):
    monkeypatch.setattr(sync_module, "REFRESH_BUDGET_WITH_CACHE_S", 0.3)
    sync = SyncService(connect(fake_portal, max_wait_s=10), make_store(), readings_ttl_s=0)
    await sync.sync_reference_data()
    fetched_at, _ = await sync.ensure_readings("J100040")

    fake_portal.fail_paths["/portal/meters/J100040/energy"] = 503
    started = time.monotonic()
    assert await sync.ensure_readings("J100040") == (fetched_at, True)
    assert time.monotonic() - started < 2  # the short budget, not the client's 10 s
    calls = fake_portal.calls["/portal/meters/J100040/energy"]
    results = await asyncio.gather(*(sync.ensure_readings("J100040") for _ in range(3)))
    assert results == [(fetched_at, True)] * 3
    assert fake_portal.calls["/portal/meters/J100040/energy"] == calls  # nobody asks again for a while


@pytest.mark.parametrize("damage", ["empty", "truncated", "ends-earlier"])
async def test_a_refresh_that_would_lose_data_keeps_the_cache(fake_portal, connect, make_store, damage):
    store = make_store()
    sync = SyncService(connect(fake_portal), store, readings_ttl_s=0)
    await sync.sync_reference_data()
    fetched_at, _ = await sync.ensure_readings("J100040")
    rows = fake_portal.energy["J100040"]
    fake_portal.energy["J100040"] = {
        "empty": [],
        "truncated": rows[:10],
        "ends-earlier": rows[:-1],
    }[damage]
    assert await sync.ensure_readings("J100040") == (fetched_at, True)
    assert store.readings_fetch_info("J100040")["reading_count"] == len(rows)


def test_a_series_with_no_parseable_reading_is_a_protocol_error(energy):
    rows = [{**r, "timestamp": "2026-06-01T00:00"} for r in energy["J100040"]]  # a format we don't know
    sync = SyncService(None, None)  # type: ignore[arg-type]  (the check needs neither)
    with pytest.raises(PortalProtocolError, match="none of the 30 readings for J100040 could be parsed"):
        sync._checked_series("J100040", rows, cached=None)


async def test_a_crawl_skips_a_page_that_keeps_failing(make_portal, connect, make_store):
    portal = make_portal(signature_tolerance_s=-1)  # export unusable: crawl
    portal.fail_paths["/meters/J100004/__data.json"] = 503
    summary = await SyncService(connect(portal, max_wait_s=0.2), make_store()).sync_reference_data()
    assert (summary["source"], summary["meters"]) == ("crawl", 19)
    assert any(w.startswith("skipped J100004: ") for w in summary["warnings"])


async def test_one_bad_record_does_not_sink_the_sync(fake_portal, connect, make_store):
    fake_portal.transformers[30]["code"] = ""  # a DT row without a code
    fake_portal.meters.append(copy.deepcopy(fake_portal.meters[0]))  # J100000 twice
    store = make_store()
    summary = await SyncService(connect(fake_portal), store).sync_reference_data()
    assert summary["meters"] == len(store.meter_ids()) == 20
    assert any(w.startswith("skipped transformer record: ") for w in summary["warnings"])
    assert "skipped a second export record for meter J100000" in summary["warnings"]


async def test_an_unchanged_portal_snapshot_is_not_rewritten(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    await sync.sync_reference_data()
    version = store.reference_version
    summary = await sync.sync_reference_data()
    assert summary["warnings"][-1] == "portal data unchanged since the last sync; snapshot kept as is"
    assert store.reference_version == version  # so the memoised views stay valid
    assert sync.reference_status()["last_run"]["status"] == "ok"  # the sync still counts as fresh

    fake_portal.meters[0]["make"] = "Changed"
    await sync.sync_reference_data()
    assert store.reference_version == version + 1


async def test_a_warm_up_counts_refreshes_that_fell_back_to_the_cache(fake_portal, connect, make_store, monkeypatch):
    # No pacing, and a short budget: the warm-up gives each refresh the client's whole budget.
    client = connect(fake_portal, rate_limit_per_minute=60_000, rate_limit_burst=1_000, max_wait_s=0.1)
    sync = SyncService(client, make_store(), readings_ttl_s=0)
    await sync.sync_reference_data()
    await sync.warm_all_readings()
    fake_portal.fail_paths.update({f"/portal/meters/{m['meterId']}/energy": 503 for m in fake_portal.meters})
    await sync.warm_all_readings()
    assert {k: sync.warmup[k] for k in ("state", "done", "failed", "errors")} == {
        "state": "done",
        "done": 20,
        "failed": 20,
        "errors": {"PortalUnavailable": 20},
    }


async def test_a_warm_up_stops_when_the_portal_refuses_our_login(fake_portal, connect, make_store):
    client = connect(fake_portal, rate_limit_per_minute=60_000, rate_limit_burst=1_000)  # no pacing
    sync = SyncService(client, make_store(), readings_ttl_s=0)
    await sync.sync_reference_data()
    await sync.warm_all_readings()
    logins = fake_portal.calls["/login"]
    fake_portal.password = "rotated"
    fake_portal.expire_all_sessions()
    await sync.warm_all_readings()
    assert (sync.warmup["state"], sync.warmup["done"], sync.warmup["errors"]) == (
        "aborted",
        0,
        {"PortalAuthError": 1},
    )
    assert fake_portal.calls["/login"] == logins + 1  # one refused login, then nothing more


async def test_a_failed_first_sync_is_retried_soon(fake_portal, connect, make_store, monkeypatch):
    sync = SyncService(connect(fake_portal, max_wait_s=0.1), make_store(), sync_interval_s=900, warm_readings=False)
    fake_portal.fail_paths["/portal/dts"] = 503
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        if seconds:
            delays.append(seconds)
            if len(delays) == 3:
                fake_portal.fail_paths.clear()  # the portal recovers
            if len(delays) == 4:
                raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(sync_module.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        await sync._sync_loop()
    # Quick retries while there is nothing to serve; the normal interval once there is.
    assert delays == [5, 10, 20, 900]


async def test_requests_queued_behind_a_failed_fetch_share_its_outcome(fake_portal, connect, make_store):
    sync = SyncService(connect(fake_portal, max_wait_s=0.2), make_store())
    await sync.sync_reference_data()
    path = "/portal/meters/J100040/energy"
    fake_portal.fail_paths[path] = 503
    results = await asyncio.gather(*(sync.ensure_readings("J100040") for _ in range(3)), return_exceptions=True)
    assert [type(r) for r in results] == [PortalUnavailable] * 3  # nothing cached to fall back on
    assert fake_portal.calls[path] == 1  # one retry cycle, not one per waiter
    with pytest.raises(PortalUnavailable):
        await sync.ensure_readings("J100040")  # a request that arrives later does ask again
    assert fake_portal.calls[path] == 2


async def test_our_own_queue_being_full_is_not_remembered_as_a_portal_failure(
    fake_portal, connect, make_store, monkeypatch
):
    client = connect(fake_portal)
    sync = SyncService(client, make_store(), readings_ttl_s=0)
    await sync.sync_reference_data()
    fetched_at, _ = await sync.ensure_readings("J100040")
    fetch = client.get_meter_energy

    async def queue_full(*args, **kwargs):
        raise RequestQueueFull("too many requests queued", retry_after=1.0)

    monkeypatch.setattr(client, "get_meter_energy", queue_full)
    assert await sync.ensure_readings("J100040") == (fetched_at, True)  # this request gets the copy...
    monkeypatch.setattr(client, "get_meter_energy", fetch)
    assert (await sync.ensure_readings("J100040"))[1] is False  # ...and the next one asks the portal


async def test_a_shorter_series_is_accepted_once_the_portal_keeps_sending_it(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store, readings_ttl_s=0)
    await sync.sync_reference_data()
    await sync.ensure_readings("J100040")
    fake_portal.energy["J100040"] = fake_portal.energy["J100040"][:-1]  # the portal withdraws the last reading
    outcomes = [(await sync.ensure_readings("J100040", force=True))[1] for _ in range(3)]
    assert outcomes == [True, True, False]  # refused twice, then taken as the portal's data
    assert store.readings_fetch_info("J100040")["reading_count"] == 29


@pytest.mark.parametrize("damage", ["renamed-keys", "unreadable-values"])
async def test_a_series_we_cannot_read_never_replaces_the_cache(fake_portal, connect, make_store, damage):
    store = make_store()
    sync = SyncService(connect(fake_portal), store, readings_ttl_s=0)
    await sync.sync_reference_data()
    fetched_at, _ = await sync.ensure_readings("J100040")
    rows = fake_portal.energy["J100040"]
    fake_portal.energy["J100040"] = {
        "renamed-keys": [{"timestamp": r["timestamp"], "energyKwh": r["kwh"], "voltR": r["voltR"]} for r in rows],
        "unreadable-values": [{**r, "kwh": f"{r['kwh']} kWh"} for r in rows],
    }[damage]
    for _ in range(4):  # format drift is never "accepted after a while"
        assert await sync.ensure_readings("J100040", force=True) == (fetched_at, True)
    kept = [r["kwh"] for r in store.get_readings("J100040")]
    assert len(kept) == 30 and None not in kept


def test_many_unreadable_timestamps_are_drift_but_one_is_just_a_bad_row(energy):
    sync = SyncService(None, None)  # type: ignore[arg-type]
    rows = energy["J100040"]
    many = [{**r, "timestamp": "31/02/2026 00:00"} if i % 5 == 0 else r for i, r in enumerate(rows)]
    with pytest.raises(PortalProtocolError, match=r"6 of the 30 readings .* unreadable timestamp"):
        sync._checked_series("J100040", many, cached=None)
    one = [{**rows[0], "timestamp": "31/02/2026 00:00"}, *rows[1:4]]  # a short series with one bad row
    assert len(sync._checked_series("J100040", one, cached=None)) == 3


def test_a_timestamp_of_another_type_is_unreadable_not_a_crash(energy):
    sync = SyncService(None, None)  # type: ignore[arg-type]
    rows = energy["J100040"]
    epoch = [{**r, "timestamp": 1780252200 + 86400 * i} for i, r in enumerate(rows)]  # the format changed
    with pytest.raises(PortalProtocolError, match="none of the 30 readings for J100040 could be parsed"):
        sync._checked_series("J100040", epoch, cached=None)
    for odd in (1780252200, 1780252200.5, True, ["01/06/2026 00:00"], {"date": "01/06/2026"}):
        assert len(sync._checked_series("J100040", [{**rows[0], "timestamp": odd}, *rows[1:4]], cached=None)) == 3


async def test_a_meter_whose_register_is_blank_is_served_not_refused(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    await sync.sync_reference_data()
    rows = fake_portal.energy["J100040"]
    blank = [{**r, "kwh": "", "kvah": ""} for r in rows]  # a dead register, not drift
    fake_portal.energy["J100040"] = blank
    assert (await sync.ensure_readings("J100040"))[1] is False
    assert [r["kwh"] for r in store.get_readings("J100040")] == [None] * 30
    # ...and it stays served: blank again, or blank with a new reading, replaces no cached value.
    assert (await sync.ensure_readings("J100040", force=True))[1] is False
    fake_portal.energy["J100040"] = [*blank, {**blank[-1], "timestamp": "01/07/2026 00:00"}]
    assert (await sync.ensure_readings("J100040", force=True))[1] is False
    assert store.readings_fetch_info("J100040")["reading_count"] == 31
    fake_portal.energy["J100040"] = blank[:10]  # but a blank series cut short is still a lesser one
    assert (await sync.ensure_readings("J100040", force=True))[1] is True


async def test_blank_registers_replace_cached_values_only_once_the_portal_insists(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store, readings_ttl_s=0)
    await sync.sync_reference_data()
    await sync.ensure_readings("J100040")
    fake_portal.energy["J100040"] = [{**r, "kwh": "", "kvah": ""} for r in fake_portal.energy["J100040"]]
    assert [(await sync.ensure_readings("J100040", force=True))[1] for _ in range(3)] == [True, True, False]


async def test_a_reading_dated_in_the_future_is_dropped_not_the_series(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    await sync.sync_reference_data()
    rows = fake_portal.energy["J100040"]
    fake_portal.energy["J100040"] = [*rows, {**rows[-1], "timestamp": "01/01/2031 00:00"}]
    assert (await sync.ensure_readings("J100040"))[1] is False
    info = store.readings_fetch_info("J100040")
    assert (info["reading_count"], info["last_ts"]) == (30, "2026-06-30T00:00:00+05:30")


async def test_a_full_queue_is_shared_with_waiters_but_never_an_older_error(
    fake_portal, connect, make_store, monkeypatch
):
    client = connect(fake_portal, max_wait_s=0.2)
    sync = SyncService(client, make_store())
    await sync.sync_reference_data()
    path = "/portal/meters/J100040/energy"
    fake_portal.fail_paths[path] = 503
    with pytest.raises(PortalUnavailable):
        await sync.ensure_readings("J100040")  # an earlier outage, nothing cached
    fake_portal.fail_paths.clear()
    fetches = 0

    async def queue_full(*args, **kwargs):
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.01)
        raise RequestQueueFull("too many requests queued", retry_after=1.0)

    monkeypatch.setattr(client, "get_meter_energy", queue_full)
    results = await asyncio.gather(*(sync.ensure_readings("J100040") for _ in range(3)), return_exceptions=True)
    assert [type(r) for r in results] == [RequestQueueFull] * 3  # not the outage from before
    assert fetches == 1  # and the waiters didn't each repeat the wait


async def test_a_reader_does_not_wait_out_a_background_fetch(fake_portal, connect, make_store, monkeypatch):
    monkeypatch.setattr(sync_module, "REFRESH_BUDGET_WITH_CACHE_S", 0.1)
    client = connect(fake_portal)
    sync = SyncService(client, make_store(), readings_ttl_s=0)
    await sync.sync_reference_data()
    fetched_at, _ = await sync.ensure_readings("J100040")
    release = asyncio.Event()
    fetch = client.get_meter_energy

    async def slow(*args, **kwargs):  # the warm-up's fetch, taking its time against a slow portal
        await release.wait()
        return await fetch(*args, **kwargs)

    monkeypatch.setattr(client, "get_meter_energy", slow)
    warm_up = asyncio.create_task(sync.ensure_readings("J100040", background=True))
    await asyncio.sleep(0.01)
    started = time.monotonic()
    assert await sync.ensure_readings("J100040") == (fetched_at, True)  # the copy, after the short budget
    assert time.monotonic() - started < 1
    release.set()
    assert (await warm_up)[1] is False


async def test_a_crawl_that_cannot_reach_a_page_keeps_the_snapshot(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal, max_wait_s=0.2), store)
    await sync.sync_reference_data()
    fake_portal.signature_tolerance_s = -1  # export unusable from now on: crawl
    fake_portal.fail_paths["/meters/J100004/__data.json"] = 503
    with pytest.raises(PortalUnavailable, match="crawl incomplete"):
        await sync.sync_reference_data()
    assert len(store.meter_ids()) == 20  # J100004 is unreachable, not gone
    assert sync.reference_status()["last_run"]["status"] == "failed"


async def test_a_crawl_gives_up_once_the_pages_are_clearly_down(make_portal, connect, make_store):
    portal = make_portal(signature_tolerance_s=-1)
    for meter in portal.meters:
        portal.fail_paths[f"/meters/{meter['meterId']}/__data.json"] = 503
    with pytest.raises(PortalUnavailable, match="crawl incomplete"):
        await SyncService(connect(portal, max_wait_s=0.2), make_store()).sync_reference_data()
    asked = sum(n for path, n in portal.calls.items() if path.endswith("/__data.json"))
    assert asked < 12  # not all 20: it stopped after a handful of outages


async def test_an_export_that_loses_its_coordinates_is_rejected(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    await sync.sync_reference_data()
    for meter in fake_portal.meters:
        meter["location"] = meter.pop("geo")  # format drift: the coordinates move to another key
    with pytest.raises(SyncRejected, match=r"0 meters with a location \(index has 20\)"):
        await sync.sync_reference_data()
    assert store.get_meter("J100000")["latitude"] is not None


async def test_a_transformer_list_that_no_longer_covers_our_meters_is_rejected(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    await sync.sync_reference_data()
    rated = {r["code"]: r["capacity_kva"] for r in store.list_transformers()}
    fake_portal.transformers = fake_portal.transformers[:4]  # of the 11 our meters use, 3 are left
    with pytest.raises(SyncRejected, match=r"rates 3 of the 11 transformers in use \(it was 11 of 11\)"):
        await sync.sync_reference_data()
    assert {r["code"]: r["capacity_kva"] for r in store.list_transformers()} == rated


async def test_a_transformer_list_left_with_only_unrated_rows_is_rejected(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    await sync.sync_reference_data()
    unrated = {r["code"] for r in store.list_transformers()[:5]}
    for row in fake_portal.transformers:  # some transformers never had a rating on the portal's list
        if row["code"] in unrated:
            row["capacityKva"] = None
    await sync.sync_reference_data()
    assert store.snapshot_counts()["rated"] == 6
    fake_portal.transformers = [row for row in fake_portal.transformers if row["code"] in unrated]
    with pytest.raises(SyncRejected, match=r"rates 0 of the 11 transformers in use \(it was 6 of 11\)"):
        await sync.sync_reference_data()  # the rows that are left carry no rating: all six would be lost
    assert store.snapshot_counts()["rated"] == 6


async def test_meters_moving_onto_fewer_transformers_is_not_a_truncated_list(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store)
    await sync.sync_reference_data()
    home = fake_portal.meters[0]
    for meter in fake_portal.meters:  # every meter is re-homed onto one transformer
        meter["dtCode"], meter["hierarchy"] = home["dtCode"], home["hierarchy"]
    summary = await sync.sync_reference_data()
    assert (summary["meters"], summary["transformers"]) == (20, 1)


async def test_an_unchanged_sync_still_restamps_the_snapshot(fake_portal, connect, make_store):
    now = [datetime(2026, 10, 1, 12, tzinfo=UTC)]
    store = make_store()
    sync = SyncService(connect(fake_portal), store, clock=lambda: now[0])
    await sync.sync_reference_data()
    now[0] += timedelta(hours=6)
    await sync.sync_reference_data()  # nothing changed on the portal
    assert store.get_meter("J100000")["synced_at"] == now[0].isoformat()
    assert store.get_transformer("DT-001")["synced_at"] == now[0].isoformat()


async def test_stopping_cancels_a_sync_in_flight_and_records_it(fake_portal, connect, make_store):
    store = make_store()
    sync = SyncService(connect(fake_portal), store, warm_readings=False)
    release = asyncio.Event()
    export = sync.client.export_meters

    async def slow_export():
        await release.wait()
        return await export()

    sync.client.export_meters = slow_export  # type: ignore[method-assign]
    sync.start()
    while not fake_portal.calls["/portal/dts"]:  # the first sync is under way, waiting on the export
        await asyncio.sleep(0.01)
    await sync.stop()
    assert sync._sync_task is not None and sync._sync_task.cancelled()
    run = store.last_sync_runs(1)[0]
    assert (run["status"], run["error"]) == ("failed", "cancelled: the service was stopping")
