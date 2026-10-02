"""The real `PortalClient` against the in-process FakePortal: sessions, throttling, retries, signing."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fake_portal import BASE_URL, EMAIL, PASSWORD, devalue_flatten

from urja_api.portal import client as client_module
from urja_api.portal.client import PortalClient
from urja_api.portal.errors import (
    PortalAuthError,
    PortalError,
    PortalNotFound,
    PortalProtocolError,
    PortalRateLimited,
    PortalUnavailable,
    RequestQueueFull,
)
from urja_api.portal.ratelimit import TokenBucket


class FlakyTransport(httpx.AsyncBaseTransport):
    """Fails the next `failures` requests with a connection error, then delegates."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self.inner = inner
        self.failures = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.failures:
            self.failures -= 1
            raise httpx.ConnectError("connection reset", request=request)
        return await self.inner.handle_async_request(request)


class StubTransport(httpx.AsyncBaseTransport):
    """Answers the given paths with canned responses; everything else reaches the fake portal."""

    def __init__(self, inner: httpx.AsyncBaseTransport, responses: dict[str, Any]) -> None:
        self.inner = inner
        self.responses = responses  # path -> a response, or a function of the request returning one
        self.hits: Counter[str] = Counter()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.hits[request.url.path] += 1
        stub = self.responses.get(request.url.path)
        if callable(stub):
            stub = stub(request)
        if stub is None:
            return await self.inner.handle_async_request(request)
        return httpx.Response(stub.status_code, headers=stub.headers, content=stub.content, request=request)


class HangingTransport(httpx.AsyncBaseTransport):
    """Never answers requests whose path starts with `prefix` (a black-holed connection)."""

    def __init__(self, inner: httpx.AsyncBaseTransport, prefix: str) -> None:
        self.inner = inner
        self.prefix = prefix

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(self.prefix):
            await asyncio.sleep(3600)
        return await self.inner.handle_async_request(request)


# ----------------------------------------------------------------------------- sessions


async def test_login_then_paged_transformer_list(fake_portal, connect):
    client = connect(fake_portal)
    rows = await client.list_transformers()
    assert [r["code"] for r in rows] == [f"DT-{n:03d}" for n in range(1, 41)]
    assert fake_portal.calls["/login"] == 1
    assert fake_portal.calls["/portal/dts"] == 2
    assert len(fake_portal.sessions) == 1
    assert client.stats.logins == 1
    assert client.stats.session_expires_at == pytest.approx(time.time() + 3600, abs=5)  # from Max-Age


async def test_bad_credentials_surface_the_portals_message(fake_portal, connect):
    client = connect(fake_portal, password="not-the-password")
    with pytest.raises(PortalAuthError, match="Invalid email or password"):
        await client.search_meters()
    assert fake_portal.sessions == {}


async def test_login_form_rejected_by_the_csrf_check(fake_portal, connect):
    client = connect(fake_portal, base_url="https://elsewhere.test")  # sends a foreign Origin
    with pytest.raises(PortalAuthError, match="Origin"):
        await client.search_meters()


async def test_expired_session_on_a_json_endpoint_logs_in_again(fake_portal, connect):
    client = connect(fake_portal)
    await client.search_meters()
    fake_portal.expire_all_sessions()
    assert (await client.search_meters("J100000"))["data"][0]["meterId"] == "J100000"
    assert fake_portal.calls["/portal/meters/search"] == 3  # 401, then the retry
    assert client.stats.logins == 2


async def test_expired_session_behind_a_soft_redirect_logs_in_again(fake_portal, connect):
    client = connect(fake_portal)
    await client.get_meter_page("J100000")
    fake_portal.expire_all_sessions()
    assert (await client.get_meter_page("J100004"))["meterId"] == "J100004"
    assert fake_portal.calls["/meters/J100004/__data.json"] == 2  # HTTP 200 {"type": "redirect"}, then data
    assert client.stats.logins == 2


async def test_concurrent_requests_after_expiry_share_one_login(fake_portal, connect, export_records):
    client = connect(fake_portal, rate_limit_per_minute=60_000, rate_limit_burst=1_000)  # searches are budgeted
    await client.search_meters()
    fake_portal.expire_all_sessions()
    ids = [r["meterId"] for r in export_records]
    pages = await asyncio.gather(*(client.get_meter_page(i) for i in ids), *(client.search_meters(i) for i in ids))
    assert [p["meterId"] for p in pages[: len(ids)]] == ids
    assert fake_portal.calls["/login"] == 2
    assert client.stats.logins == 2
    assert len(fake_portal.sessions) == 1


async def test_session_is_renewed_before_the_portal_expires_it(fake_portal, connect, monkeypatch):
    client = connect(fake_portal)
    await client.search_meters()
    later = time.monotonic() + 3600 - 60  # inside the renewal margin, before the portal's expiry
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: later, time=time.time))
    await client.search_meters()
    assert fake_portal.calls["/login"] == 2
    assert fake_portal.calls["/portal/meters/search"] == 2  # renewed up front, not after a 401


async def test_aclose_signs_out(fake_portal):
    client = PortalClient(BASE_URL, EMAIL, PASSWORD, transport=httpx.ASGITransport(app=fake_portal.app))
    await client.search_meters()
    assert len(fake_portal.sessions) == 1
    await client.aclose()
    assert fake_portal.sessions == {}
    assert fake_portal.calls["/api/auth/sign-out"] == 1


async def test_aclose_without_a_session_does_not_call_the_portal(fake_portal):
    client = PortalClient(BASE_URL, EMAIL, PASSWORD, transport=httpx.ASGITransport(app=fake_portal.app))
    await client.aclose()
    assert sum(fake_portal.calls.values()) == 0


async def test_aclose_twice_signs_out_once(fake_portal, connect):
    client = connect(fake_portal)
    await client.search_meters()
    await client.aclose()
    await client.aclose()  # and a third time when the fixture tears down
    assert fake_portal.calls["/api/auth/sign-out"] == 1
    assert fake_portal.sessions == {}


@pytest.mark.parametrize(
    ("ttl_s", "kept_at", "renewed_at"),
    [
        (600, 500, 545),  # margin 60 s (10 %): a flat 120 s margin would already renew at 500 s
        (3600, 3400, 3500),  # margin 120 s: a flat 10 % margin (360 s) would already renew at 3400 s
    ],
    ids=["short-session-tenth", "long-session-two-minutes"],
)
async def test_the_renewal_margin_is_the_smaller_of_two_minutes_and_a_tenth(
    make_portal, connect, monkeypatch, ttl_s, kept_at, renewed_at
):
    portal = make_portal(session_ttl_s=ttl_s)
    client = connect(portal)
    start = time.monotonic()
    await client.search_meters()

    async def search_at(offset_s: float) -> None:
        clock = SimpleNamespace(monotonic=lambda: start + offset_s, time=time.time)
        monkeypatch.setattr(client_module, "time", clock)
        await client.search_meters()

    await search_at(kept_at)
    assert portal.calls["/login"] == 1
    await search_at(renewed_at)
    assert portal.calls["/login"] == 2


# ----------------------------------------------------------------------------- login failures


async def test_a_login_server_error_is_retried_within_the_request(fake_portal, connect, fast_backoff):
    client = connect(fake_portal)
    fake_portal.fail_next = [503]  # the first request out is the login POST
    assert (await client.search_meters("J100000"))["data"][0]["meterId"] == "J100000"
    assert fake_portal.calls["/login"] == 2
    assert (client.stats.logins, client.stats.upstream_errors, client.stats.retries) == (1, 1, 1)


async def test_a_login_network_error_is_retried_within_the_request(fake_portal, fast_backoff):
    transport = FlakyTransport(httpx.ASGITransport(app=fake_portal.app))
    transport.failures = 1  # the login POST never reaches the portal
    client = PortalClient(BASE_URL, EMAIL, PASSWORD, transport=transport)
    try:
        assert (await client.get_meter_page("J100000"))["meterId"] == "J100000"
        assert fake_portal.calls["/login"] == 1
        assert (client.stats.logins, client.stats.retries) == (1, 1)
    finally:
        await client.aclose()


async def test_rejected_credentials_pause_logins_for_a_cool_down(fake_portal, connect, monkeypatch):
    client = connect(fake_portal, password="not-the-password")
    with pytest.raises(PortalAuthError, match="Invalid email or password"):
        await client.search_meters()

    # Within the cool-down, requests fail at once without posting the login form again.
    results = await asyncio.gather(client.get_meter_page("J100000"), client.search_meters(), return_exceptions=True)
    assert all(isinstance(r, PortalAuthError) and "login paused" in str(r) for r in results), results
    assert dict(fake_portal.calls) == {"/login": 1}

    after = time.monotonic() + client_module.LOGIN_COOLDOWN_S + 1
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: after, time=time.time))
    with pytest.raises(PortalAuthError, match="Invalid email or password"):
        await client.search_meters()
    assert dict(fake_portal.calls) == {"/login": 2}  # tried again once the cool-down is over


@pytest.mark.parametrize("method", ["search_meters", "get_meter_page"], ids=["401", "soft-redirect"])
async def test_a_freshly_issued_session_that_is_refused_is_an_auth_error(fake_portal, connect, method):
    fake_portal._session_ok = lambda request: False  # sessions are issued, but never honoured
    client = connect(fake_portal)
    with pytest.raises(PortalAuthError, match="freshly issued session"):
        async with asyncio.timeout(5):  # a regression here would loop on re-login forever
            await getattr(client, method)("J100000")
    assert fake_portal.calls["/login"] == 2  # one re-login, then it gives up


# ----------------------------------------------------------------------------- throttling and retries


async def test_rate_limit_pauses_then_succeeds(make_portal, connect, fast_backoff):
    now = [time.time()]
    portal = make_portal(rate_limit=2, clock=lambda: now[0])  # the portal's window only moves when we say so
    client = connect(portal, rate_limit_per_minute=60_000)
    await client.get_meter_energy("J100000")
    await client.get_meter_energy("J100004")

    async def window_reopens_after_the_first_429() -> None:
        while not client.stats.rate_limited:
            await asyncio.sleep(0)
        now[0] += portal.rate_window_s

    readings, _ = await asyncio.gather(client.get_meter_energy("J100006"), window_reopens_after_the_first_429())
    assert readings
    assert (client.stats.rate_limited, client.stats.retries) == (1, 1)
    assert portal.calls["/portal/meters/J100006/energy"] == 2


async def test_persistent_rate_limit_gives_up_with_retry_after(make_portal, connect):
    portal = make_portal(rate_limit=0)
    client = connect(portal, max_wait_s=1)
    with pytest.raises(PortalRateLimited) as caught:
        await client.get_meter_energy("J100000")
    assert 3.2 <= caught.value.retry_after <= 4.8  # 4 s ± 20 % jitter, beyond the 1 s budget
    assert client.stats.rate_limited == 1

    # The pause is global: other /portal/* calls fail fast without reaching the portal...
    with pytest.raises(PortalRateLimited):
        await client.search_meters("J100004")
    assert portal.calls["/portal/meters/search"] == 0
    # ...while endpoints the portal does not rate limit carry on.
    assert (await client.get_meter_page("J100004"))["meterId"] == "J100004"


async def test_server_errors_are_retried(fake_portal, connect, fast_backoff):
    client = connect(fake_portal)
    await client.search_meters()
    fake_portal.fail_next = [500, 502, 503]
    assert len(await client.list_transformers()) == 40
    assert (client.stats.upstream_errors, client.stats.retries) == (3, 3)


async def test_persistent_server_errors_raise_unavailable(fake_portal, connect, fast_backoff):
    client = connect(fake_portal, max_wait_s=0.1)
    await client.search_meters()
    fake_portal.fail_next = [503] * 1_000
    with pytest.raises(PortalUnavailable, match="HTTP 503"):
        await client.search_meters("J100000")
    assert fake_portal.calls["/portal/meters/search"] > 3  # retried until the deadline
    assert client.stats.last_error == "GET /portal/meters/search: HTTP 503"


async def test_network_errors_are_retried(fake_portal, fast_backoff):
    transport = FlakyTransport(httpx.ASGITransport(app=fake_portal.app))
    client = PortalClient(BASE_URL, EMAIL, PASSWORD, transport=transport)
    try:
        await client.search_meters()
        transport.failures = 2
        assert (await client.get_meter_page("J100000"))["meterId"] == "J100000"
        assert client.stats.retries == 2
    finally:
        await client.aclose()


@pytest.mark.parametrize("prefix", ["/portal/", "/login"], ids=["request", "login"])
async def test_a_hung_connection_is_cut_at_the_deadline(fake_portal, prefix):
    # The HTTP timeout alone (30 s here) would let one hung call outlive the 0.2 s budget.
    transport = HangingTransport(httpx.ASGITransport(app=fake_portal.app), prefix)
    client = PortalClient(BASE_URL, EMAIL, PASSWORD, timeout_s=30, max_wait_s=0.2, transport=transport)
    try:
        started = time.monotonic()
        with pytest.raises(PortalUnavailable, match=r"within 0\.2s"):
            await client.search_meters()
        assert time.monotonic() - started < 0.2 + client_module.DEADLINE_GRACE_S + 0.5
    finally:
        await client.aclose()


async def test_every_portal_call_spends_the_shared_budget(fake_portal, connect):
    # Four tokens and practically no refill: the fifth /portal/* call cannot go out in time.
    client = connect(fake_portal, rate_limit_per_minute=1, rate_limit_burst=4, max_wait_s=0.05)
    for meter_id in ("J100000", "J100004", "J100162"):
        await client.get_meter_page(meter_id)  # detail pages (like the login they start with) are not budgeted
    await client.search_meters()
    await client.get_meter_energy("J100000")
    await client.export_meters()  # /portal/keys, then /portal/export
    with pytest.raises(RequestQueueFull) as caught:  # our own queue, not the portal
        await client.list_transformers()
    assert caught.value.retry_after >= 1
    assert fake_portal.calls["/portal/dts"] == 0
    assert client.stats.rate_limited == 0  # our own throttle said no, not the portal


async def test_an_outage_cut_short_by_our_own_throttle_is_reported_as_an_outage(fake_portal, connect, fast_backoff):
    client = connect(fake_portal, rate_limit_per_minute=1, rate_limit_burst=1, max_wait_s=0.05)
    await client.get_meter_page("J100000")  # log in without spending the only token
    fake_portal.fail_next = [503]
    with pytest.raises(PortalUnavailable, match="HTTP 503"):
        await client.search_meters()  # the retry waits for a token until the deadline
    assert fake_portal.calls["/portal/meters/search"] == 1
    assert (client.stats.upstream_errors, client.stats.rate_limited) == (1, 0)
    assert client.stats.last_error == "GET /portal/meters/search: HTTP 503"


async def test_a_429_with_a_back_off_past_the_deadline_is_a_rate_limit_even_after_an_outage(
    make_portal, connect, monkeypatch
):
    jitter = iter([0.001, 1.0])  # a near-instant 5xx retry, then the full 429 back-off
    monkeypatch.setattr(client_module, "random", SimpleNamespace(uniform=lambda low, high: next(jitter)))
    portal = make_portal(rate_limit=0)  # every /portal/* call is answered 429
    client = connect(portal, max_wait_s=1)
    await client.get_meter_page("J100000")  # log in first, so the injected 503 hits the energy call
    portal.fail_next = [503]
    with pytest.raises(PortalRateLimited) as caught:
        await client.get_meter_energy("J100000")
    assert caught.value.retry_after == 8.0  # 4 s doubled for the second attempt: beyond the 1 s budget
    assert portal.calls["/portal/meters/J100000/energy"] == 2
    assert (client.stats.upstream_errors, client.stats.rate_limited) == (1, 1)


async def test_a_throttle_timeout_after_a_429_is_not_reported_as_the_earlier_outage(make_portal, connect, fast_backoff):
    portal = make_portal(rate_limit=0)  # every /portal/* call is answered 429
    # Two tokens: one for the attempt that meets the 503, one for the attempt that meets the 429.
    client = connect(portal, rate_limit_per_minute=1, rate_limit_burst=2, max_wait_s=0.2)
    await client.get_meter_page("J100000")  # log in without spending a token
    portal.fail_next = [503]
    with pytest.raises(RequestQueueFull):  # the portal answered (429) since the outage
        await client.get_meter_energy("J100000")  # the third attempt waits for a token until the deadline
    assert portal.calls["/portal/meters/J100000/energy"] == 2
    assert (client.stats.upstream_errors, client.stats.rate_limited) == (1, 1)


async def test_wait_for_budget_sleeps_out_a_rate_limit_pause(make_portal, connect, monkeypatch):
    monkeypatch.setattr(client_module, "random", SimpleNamespace(uniform=lambda low, high: 1.0))  # no jitter
    now, waits = [1_000.0], []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        if seconds:
            waits.append(seconds)
            now[0] += seconds
        await real_sleep(0)

    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    portal = make_portal(rate_limit=0)  # every /portal/* call is answered 429
    client = connect(portal, max_wait_s=1)
    client._throttle = TokenBucket(100 / 60, 10, clock=lambda: now[0], sleep=fake_sleep)  # on the fake clock

    await client.wait_for_budget()
    assert waits == []  # not paused: returns at once

    with pytest.raises(PortalRateLimited):
        await client.get_meter_energy("J100000")  # the 429 pauses every budgeted call for 4 s
    await client.wait_for_budget()
    assert waits == [4.0]
    portal.rate_limit = 120  # the portal's window has rolled over too
    assert (await client.search_meters())["total"] == 20  # the pause is over


@pytest.mark.parametrize("method", ["get_meter_energy", "get_meter_page"])
async def test_unknown_meter(fake_portal, connect, method):
    client = connect(fake_portal)
    with pytest.raises(PortalNotFound, match="J999999"):
        await getattr(client, method)("J999999")


# ----------------------------------------------------------------------------- signed export


async def test_signed_export(fake_portal, connect, export_records):
    client = connect(fake_portal)
    assert await client.export_meters() == export_records
    assert await client.export_meters() == export_records
    assert fake_portal.calls["/portal/keys"] == 1  # the secret is cached
    assert fake_portal.calls["/portal/export"] == 2


async def test_rotated_signing_secret_is_fetched_again(fake_portal, connect):
    client = connect(fake_portal)
    await client.export_meters()
    fake_portal.signing_secret = "rotated-secret"
    assert len(await client.export_meters()) == 20
    assert fake_portal.calls["/portal/keys"] == 2
    assert fake_portal.calls["/portal/export"] == 3  # rejected once, then signed with the new secret


@pytest.mark.parametrize("skew_s", [200, 1000])
async def test_export_is_signed_with_the_portals_clock(make_portal, connect, skew_s):
    # A tolerance tighter than the real portal's (about ±240 s), so even +200 s needs the correction.
    portal = make_portal(clock=lambda: time.time() + skew_s, signature_tolerance_s=60)
    client = connect(portal, date_header=True)
    assert len(await client.export_meters()) == 20
    assert client.stats.clock_skew_s == pytest.approx(skew_s, abs=2)


async def test_skew_without_a_date_header_breaks_the_signature(make_portal, connect):
    portal = make_portal(clock=lambda: time.time() + 1000)
    client = connect(portal)
    with pytest.raises(PortalProtocolError):
        await client.export_meters()
    assert portal.calls["/portal/keys"] == 2  # a fresh secret was tried before giving up


# ----------------------------------------------------------------------------- unexpected answers


def stubbed(portal, responses: dict[str, httpx.Response]) -> tuple[PortalClient, StubTransport]:
    transport = StubTransport(httpx.ASGITransport(app=portal.app), responses)
    return PortalClient(BASE_URL, EMAIL, PASSWORD, transport=transport), transport


@pytest.mark.parametrize(
    "body",
    [{"rows": []}, {"data": None}, {"data": [1, 2]}, [{"timestamp": "01/06/2026 00:00"}]],
    ids=["renamed-key", "null", "not-objects", "bare-list"],
)
async def test_an_unexpected_payload_shape_is_a_protocol_error(fake_portal, body):
    client, _ = stubbed(fake_portal, {"/portal/meters/J100000/energy": httpx.Response(200, json=body)})
    try:
        with pytest.raises(PortalProtocolError):
            await client.get_meter_energy("J100000")
    finally:
        await client.aclose()


async def test_a_changed_keys_payload_never_echoes_the_secret(fake_portal):
    keys = httpx.Response(200, json={"data": {"signing_secret": "TOPSECRET-123"}})
    client, _ = stubbed(fake_portal, {"/portal/keys": keys})
    try:
        with pytest.raises(PortalProtocolError) as caught:
            await client.export_meters()
        assert "signing_secret" in str(caught.value)  # the shape helps whoever has to fix it...
        assert "TOPSECRET" not in str(caught.value)  # ...the value would leak a credential
    finally:
        await client.aclose()


async def test_a_login_answer_we_do_not_understand_also_pauses_logins(fake_portal):
    page = httpx.Response(
        200, text="<html>Down for maintenance</html>", headers={"content-type": "text/html", "set-cookie": "s=1"}
    )
    client, transport = stubbed(fake_portal, {"/login": page})
    try:
        with pytest.raises(PortalProtocolError):
            await client.search_meters()
        assert transport.hits["/api/auth/sign-out"] == 1  # whatever session it may have issued is ended
        results = await asyncio.gather(client.search_meters(), client.list_transformers(), return_exceptions=True)
        assert all(isinstance(r, PortalError) and "login paused" in str(r) for r in results), results
        assert transport.hits["/login"] == 1  # no login storm while the portal is in this state
    finally:
        await client.aclose()


async def test_a_429_outside_the_shared_budget_waits_without_pausing_it(fake_portal, connect, fast_backoff):
    client = connect(fake_portal)
    await client.search_meters()  # log in
    fake_portal.fail_next = [429]
    assert (await client.get_meter_page("J100000"))["meterId"] == "J100000"  # retried after a back-off
    assert fake_portal.calls["/meters/J100000/__data.json"] == 2
    assert client._throttle.paused_for == 0  # /portal/* calls are not held back by a page's 429


async def test_a_login_failure_never_echoes_what_the_portal_sends_back(fake_portal):
    data = json.dumps(devalue_flatten({"error": {"form": {"email": EMAIL, "password": PASSWORD}}}))
    answers = iter(
        [
            httpx.Response(200, json={"type": "failure", "status": 401, "data": data}),
            httpx.Response(200, json={"type": {"submitted": PASSWORD}}),
        ]
    )
    client, _ = stubbed(fake_portal, {"/login": lambda request: next(answers)})
    try:
        with pytest.raises(PortalAuthError, match=r"login failed \(HTTP 401\)") as caught:
            await client.search_meters()
        assert PASSWORD not in str(caught.value)
        client._login_blocked_until = 0.0  # skip the cool-down to see the second answer
        with pytest.raises(PortalProtocolError) as caught:
            await client.search_meters()
        assert PASSWORD not in str(caught.value)
    finally:
        await client.aclose()


async def test_the_cool_down_is_recorded_even_if_the_sign_out_hangs(fake_portal):
    page = httpx.Response(200, text="<html></html>", headers={"content-type": "text/html", "set-cookie": "s=1"})
    hanging = HangingTransport(httpx.ASGITransport(app=fake_portal.app), "/api/auth/sign-out")
    transport = StubTransport(hanging, {"/login": page})
    client = PortalClient(BASE_URL, EMAIL, PASSWORD, transport=transport, max_wait_s=0.2)
    try:
        for _ in range(3):
            with pytest.raises(PortalError):
                await client.search_meters()
        assert transport.hits["/login"] == 1  # the first failure paused logins before the sign-out was awaited
        assert not client._http.cookies  # and the unrecognised session is not kept
    finally:
        client._http.cookies.clear()
        await client._http.aclose()


async def test_a_refused_session_is_one_failure_however_many_callers_saw_it(fake_portal, connect, monkeypatch):
    fake_portal._session_ok = lambda request: False  # sessions are issued, but never honoured
    client = connect(fake_portal)
    results = await asyncio.gather(*(client.search_meters() for _ in range(5)), return_exceptions=True)
    assert all(isinstance(r, PortalAuthError) for r in results), results
    assert client._login_failures == 1
    assert client._login_blocked_until - time.monotonic() <= client_module.LOGIN_COOLDOWN_S

    # The next episode, after the cool-down, doubles it: a login that "works" doesn't reset the count.
    after = time.monotonic() + client_module.LOGIN_COOLDOWN_S + 1
    monkeypatch.setattr(client_module, "time", SimpleNamespace(monotonic=lambda: after, time=time.time))
    with pytest.raises(PortalAuthError):
        await client.search_meters()
    assert client._login_blocked_until - after == pytest.approx(2 * client_module.LOGIN_COOLDOWN_S)


async def test_during_a_429_episode_queued_callers_are_rate_limited_not_busy(make_portal, connect, monkeypatch):
    monkeypatch.setattr(client_module, "random", SimpleNamespace(uniform=lambda low, high: 1.0))  # no jitter
    portal = make_portal(rate_limit=0)  # every /portal/* call is answered 429
    client = connect(portal, rate_limit_per_minute=600, rate_limit_burst=2, max_wait_s=0.4)
    await client.get_meter_page("J100000")  # log in without spending a token
    meters = ["J100000", "J100001", "J100002", "J100004", "J100006", "J100010"]
    results = await asyncio.gather(*(client.get_meter_energy(m) for m in meters), return_exceptions=True)
    # Two callers met the 429 themselves; the rest were queued behind the pause it caused.
    assert [type(r) for r in results] == [PortalRateLimited] * 6
    assert client.stats.rate_limited == 2


@pytest.mark.parametrize(
    "body",
    [{"data": [{"code": "DT-001"}]}, {"data": [{"code": "DT-001"}], "total": "40"}, {"data": [], "total": 40}],
    ids=["no-total", "total-as-string", "empty-page"],
)
async def test_a_transformer_list_that_would_be_truncated_is_a_protocol_error(fake_portal, body):
    client, _ = stubbed(fake_portal, {"/portal/dts": httpx.Response(200, json=body)})
    try:
        with pytest.raises(PortalProtocolError):
            await client.list_transformers()
    finally:
        await client.aclose()


async def test_a_search_listing_that_runs_dry_early_is_a_protocol_error(fake_portal):
    def page(request: httpx.Request) -> httpx.Response:
        first = request.url.params.get("page") == "1"
        rows = [{"meterId": f"J{n}"} for n in range(20)] if first else []
        return httpx.Response(200, json={"data": rows, "total": 10**6})

    client, transport = stubbed(fake_portal, {"/portal/meters/search": page})
    try:
        with pytest.raises(PortalProtocolError, match="empty page 2"):
            await client.list_meter_ids()
        assert transport.hits["/portal/meters/search"] == 2  # not a million / 20 requests
    finally:
        await client.aclose()


async def test_a_meter_removed_while_the_listing_is_walked_is_not_an_error(fake_portal, connect):
    fake_portal.meters.append({**fake_portal.meters[0], "meterId": "J100999", "serialNo": "ZZ00001"})  # 21: two pages
    client = connect(fake_portal)
    search = client.search_meters

    async def shrinking(query: str = "", page: int = 1):
        body = await search(query, page)
        if page == 1:
            fake_portal.meters.pop()  # gone before page 2 is asked for
        return body

    client.search_meters = shrinking  # type: ignore[method-assign]
    assert len(await client.list_meter_ids()) == 20
