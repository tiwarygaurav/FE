"""Async client for the Urja Meter Ops portal.

This is the only module that knows how the portal speaks HTTP. It turns the portal's
browser-oriented behaviour into plain method calls that return decoded (but not yet
normalised) payloads, and it absorbs the operational quirks:

* **Login** is a SvelteKit form action (``POST /login``). It needs an ``Origin`` header
  (SvelteKit CSRF check), answers HTTP 200 even for bad credentials (the outcome is in the
  JSON ``type``) and issues a ``__Secure-better-auth.session_token`` cookie that lives for
  exactly one hour and is *not* extended by use. We log in lazily, renew shortly before
  expiry, serialise logins so concurrent requests don't stampede the login form, and
  pause logins after a rejected or unintelligible one instead of repeating it on every
  request (a login that fails with a 5xx or network error is retried like any outage).
* **Expired sessions** show up differently per route: ``401 {"error": "unauthorized"}`` on
  ``/portal/*``, but ``200 {"type": "redirect", "location": "/login"}`` on ``__data.json``.
  Both trigger one re-login and a retry.
* **Rate limiting**: every ``/portal/*`` call draws on one budget of 120 requests per
  60 s window, and a 429 carries no ``Retry-After``. We pace all of them through one
  token bucket and pause everything on a 429.
* **The bulk export** needs an HMAC signature; the secret is cached and refreshed if a
  signature is rejected, and timestamps are corrected for clock skew using the portal's
  ``Date`` header (the server only tolerates about ±4 minutes).

Every request runs under one overall deadline (``max_wait_s``) that covers login, lock
waits, throttling, retries and hung connections alike.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from .. import __version__
from .devalue import DevalueError, unflatten
from .errors import (
    PortalAuthError,
    PortalError,
    PortalNotFound,
    PortalProtocolError,
    PortalRateLimited,
    PortalUnavailable,
    RequestQueueFull,
)
from .ratelimit import TokenBucket
from .signing import signature_headers

log = logging.getLogger(__name__)

SESSION_COOKIE = "__Secure-better-auth.session_token"
DEFAULT_SESSION_TTL_S = 3600
# Renew the session this long before the portal would expire it.
SESSION_RENEW_MARGIN_S = 120
# After a failed login, don't try the login form again for this long (doubling per failure).
LOGIN_COOLDOWN_S, LOGIN_COOLDOWN_MAX_S = 60, 900
# The retry loop's own deadline decides how a slow call ends (rate limit, queue or outage);
# the hard timeout around it, this much later, only cuts a hung connection or login.
DEADLINE_GRACE_S = 0.25
EXPORT_PATH = "/portal/export"
_MAX_AGE_RE = re.compile(r"max-age=(\d+)", re.IGNORECASE)

HeadersFactory = Callable[[], dict[str, str]]


@dataclass
class ClientStats:
    """Counters surfaced on the service's status endpoint."""

    requests: int = 0
    retries: int = 0
    logins: int = 0
    rate_limited: int = 0
    upstream_errors: int = 0
    last_success_at: float | None = None
    last_error: str | None = None
    session_expires_at: float | None = None  # wall-clock epoch seconds
    clock_skew_s: float = 0.0


class PortalClient:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        *,
        timeout_s: float = 10.0,
        rate_limit_per_minute: int = 100,
        rate_limit_burst: int = 10,
        max_wait_s: float = 25.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._email = email
        self._password = password
        self._max_wait_s = max_wait_s
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
            headers={"user-agent": f"urja-meter-api/{__version__} (read-only integration)"},
        )
        self._throttle = TokenBucket(rate_limit_per_minute / 60, rate_limit_burst)
        self._login_lock = asyncio.Lock()
        self._session_deadline = 0.0  # time.monotonic() value after which we renew
        self._session_generation = 0
        self._login_failures = 0
        self._login_blocked_until = 0.0
        self._login_error = ""
        self._secret: str | None = None
        self._clock_skew = 0.0
        self.stats = ClientStats()

    # ------------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        """Sign out (so we don't leave live sessions behind) and close the connection pool."""
        if self._http.is_closed:
            return
        if self._session_generation and time.monotonic() < self._session_deadline:
            with contextlib.suppress(httpx.HTTPError):
                await self._http.post("/api/auth/sign-out", json={}, headers={"origin": self.base_url}, timeout=5)
        await self._http.aclose()

    # ------------------------------------------------------------------ session

    def _session_is_fresh(self) -> bool:
        return self._session_generation > 0 and time.monotonic() < self._session_deadline

    async def _ensure_session(self) -> int:
        if self._session_is_fresh():
            return self._session_generation
        async with self._login_lock:
            if not self._session_is_fresh():  # another task may have logged in meanwhile
                if time.monotonic() < self._login_blocked_until:
                    raise PortalAuthError(f"{self._login_error} (login paused after a failure)")
                try:
                    await self._login()
                except (PortalAuthError, PortalProtocolError) as exc:
                    self._block_logins(str(exc))  # recorded first: the sign-out below may be cut short
                    await self._discard_session()
                    raise
            return self._session_generation

    def _block_logins(self, reason: str) -> None:
        """Back off from the login form: 60 s after the first failure, doubling up to 15 min.

        Counted once per episode, however many callers ran into it; the count restarts
        when a request next succeeds."""
        if time.monotonic() < self._login_blocked_until:
            return
        self._login_failures += 1
        cooldown = min(LOGIN_COOLDOWN_MAX_S, LOGIN_COOLDOWN_S * 2 ** (self._login_failures - 1))
        self._login_blocked_until = time.monotonic() + cooldown
        self._login_error = reason
        self._session_deadline = 0.0

    async def _discard_session(self) -> None:
        """Best effort: end whatever session the cookie jar holds, then forget it."""
        try:
            if self._http.cookies:
                with contextlib.suppress(httpx.HTTPError):
                    await self._http.post("/api/auth/sign-out", json={}, headers={"origin": self.base_url}, timeout=5)
        finally:
            self._http.cookies.clear()

    def _invalidate_session(self, generation: int) -> None:
        # Only the first request that notices an expired session forces a re-login;
        # the others see a newer generation and simply retry.
        if generation == self._session_generation:
            self._session_deadline = 0.0

    async def _login(self) -> None:
        self._http.cookies.clear()
        try:
            resp = await self._http.post(
                "/login",
                data={"email": self._email, "password": self._password},
                headers={
                    "origin": self.base_url,  # SvelteKit rejects cross-site form posts
                    "accept": "application/json",
                    "x-sveltekit-action": "true",  # ask for the JSON action result
                },
            )
        except httpx.HTTPError as exc:
            raise PortalUnavailable(f"login request failed: {type(exc).__name__}") from exc
        self._observe_clock(resp)

        if resp.status_code == 403:
            raise PortalAuthError("portal rejected the login form (Origin/CSRF check)")
        if resp.status_code >= 500:
            raise PortalUnavailable(f"login failed with HTTP {resp.status_code}")
        body = _json(resp)
        outcome = body.get("type") if isinstance(body, dict) else None
        if outcome == "failure":
            raise PortalAuthError(_action_failure_message(body))
        if outcome != "redirect" or SESSION_COOKIE not in self._http.cookies:
            # Only the shape: a login response is not something to echo into logs or status.
            # (It may still have issued a session under a name we don't know: the caller
            # signs out whatever the cookie jar holds.)
            kind = repr(outcome) if outcome is None or (isinstance(outcome, str) and len(outcome) <= 40) else "?"
            raise PortalProtocolError(f"unexpected login response: HTTP {resp.status_code}, type={kind}")

        ttl = _cookie_max_age(resp) or DEFAULT_SESSION_TTL_S
        # Renew early, but never so early that a short-lived session is renewed on every call.
        self._session_deadline = time.monotonic() + ttl - min(SESSION_RENEW_MARGIN_S, ttl * 0.1)
        self._session_generation += 1
        self.stats.logins += 1
        self.stats.session_expires_at = time.time() + ttl
        log.info("logged in to portal (session valid for %ss)", ttl)

    # ------------------------------------------------------------------ transport

    def _observe_clock(self, resp: httpx.Response) -> None:
        header = resp.headers.get("date")
        if not header:
            return
        try:
            server_now = parsedate_to_datetime(header).timestamp()
        except (TypeError, ValueError):
            return
        self._clock_skew = server_now - time.time()
        self.stats.clock_skew_s = round(self._clock_skew, 1)

    def _server_time(self) -> int:
        return int(time.time() + self._clock_skew)

    async def _get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers_factory: HeadersFactory | None = None,
        max_wait_s: float | None = None,
    ) -> httpx.Response:
        """GET with session management, throttling and bounded retries.

        Returns the final response for 2xx and 4xx (other than auth/429) statuses. The
        whole call, login and waits included, is bounded by the client's `max_wait_s`, or by
        the shorter one given here; past it, `PortalUnavailable`, `PortalRateLimited` or
        `RequestQueueFull` is raised.
        `headers_factory` is called per attempt so signatures carry a fresh timestamp.
        """
        budget = self._max_wait_s if max_wait_s is None else min(max_wait_s, self._max_wait_s)
        failures: list[str] = []
        try:
            async with asyncio.timeout(budget + DEADLINE_GRACE_S):
                return await self._retrying_get(path, params, headers_factory, time.monotonic() + budget, failures)
        except TimeoutError:
            earlier = f" (earlier: {failures[-1]})" if failures else ""
            self._record_error(f"GET {path}: no answer within {budget:g}s{earlier}")
            raise PortalUnavailable(f"portal did not answer GET {path} within {budget:g}s{earlier}") from None

    async def _retrying_get(
        self,
        path: str,
        params: dict[str, Any] | None,
        headers_factory: HeadersFactory | None,
        deadline: float,
        failures: list[str],
    ) -> httpx.Response:
        budgeted = path.startswith("/portal/")  # every /portal/* call spends the shared budget
        attempt = 0
        reauthenticated = False
        outage: str | None = None  # the last network error / 5xx we are retrying through
        while True:
            try:
                generation, outcome = await self._attempt(path, params, headers_factory, deadline, budgeted)
            except (PortalRateLimited, RequestQueueFull):
                if outage is None:
                    raise
                # Our own throttle ran out the clock while we retried an outage: report the
                # outage, not a rate limit the portal never imposed.
                self._record_error(f"GET {path}: {outage}")
                raise PortalUnavailable(f"portal unavailable for GET {path}: {outage}") from None
            if isinstance(outcome, str):
                failure = outcome
            else:
                resp = outcome
                if _is_auth_failure(resp):
                    if reauthenticated:
                        self._block_logins(f"portal refused a freshly issued session for {path}")
                        await self._discard_session()
                        raise PortalAuthError(f"portal refused a freshly issued session for {path}")
                    reauthenticated = True
                    self._invalidate_session(generation)
                    continue
                if resp.status_code == 429:
                    outage = None
                    failures.append("HTTP 429")
                    self.stats.rate_limited += 1
                    # No Retry-After from the portal: its window is ~60 s, so back off
                    # exponentially up to that. A 429 on the shared budget holds back every
                    # /portal/* call; anywhere else only this request waits.
                    pause = min(60.0, 4.0 * 2**attempt) * random.uniform(0.8, 1.2)
                    if budgeted:
                        self._throttle.pause(pause)
                    if time.monotonic() + pause > deadline:
                        self._record_error(f"GET {path}: portal rate limit exceeded")
                        raise PortalRateLimited("portal rate limit exceeded", retry_after=pause)
                    if not budgeted:
                        await asyncio.sleep(pause)
                    attempt += 1
                    self.stats.retries += 1
                    continue
                if resp.status_code < 500:
                    self.stats.last_success_at = time.time()
                    self._login_failures = 0  # the session works: the next failure starts a new count
                    return resp
                failure = f"HTTP {resp.status_code}"

            # Transient failure (network error or 5xx): retry with jittered backoff.
            outage = failure
            failures.append(failure)
            self.stats.upstream_errors += 1
            delay = min(8.0, 0.5 * 2**attempt) * random.uniform(0.8, 1.2)
            if time.monotonic() + delay > deadline:
                self._record_error(f"GET {path}: {failure}")
                raise PortalUnavailable(f"portal unavailable for GET {path}: {failure}")
            log.warning("GET %s failed (%s); retrying in %.1fs", path, failure, delay)
            attempt += 1
            self.stats.retries += 1
            await asyncio.sleep(delay)

    async def _attempt(
        self,
        path: str,
        params: dict[str, Any] | None,
        headers_factory: HeadersFactory | None,
        deadline: float,
        budgeted: bool,
    ) -> tuple[int, httpx.Response | str]:
        """One try: make sure we're logged in, wait for rate budget, send.

        Transient failures (a network error, or the login itself failing with a 5xx) come
        back as a string so the caller can retry them like any other outage.
        """
        try:
            generation = await self._ensure_session()
        except PortalUnavailable as exc:
            return 0, str(exc)
        if budgeted:
            await self._acquire_token(deadline)
        headers = headers_factory() if headers_factory else None
        self.stats.requests += 1
        try:
            resp = await self._http.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:  # transport errors, timeouts, undecodable bodies
            return generation, type(exc).__name__
        self._observe_clock(resp)
        return generation, resp

    async def _acquire_token(self, deadline: float) -> None:
        # Fast path: a free token is never refused, even if login ate the whole deadline.
        if self._throttle.try_acquire():
            return
        wait = self._throttle.paused_for
        if time.monotonic() + wait > deadline:
            raise PortalRateLimited("waiting for the portal rate limit window", retry_after=wait)
        try:
            await asyncio.wait_for(self._throttle.acquire(), timeout=max(0.0, deadline - time.monotonic()))
        except TimeoutError as exc:
            if (paused := self._throttle.paused_for) > 0:  # held back by the portal's 429s
                raise PortalRateLimited("portal rate limit exceeded", retry_after=paused) from exc
            # Our own queue, not the portal: tell the caller how long the backlog takes to drain.
            raise RequestQueueFull(
                "too many requests queued for the portal's rate budget",
                retry_after=max(1.0, self._throttle.waiting / self._throttle.rate, self._throttle.paused_for),
            ) from exc

    def _record_error(self, message: str) -> None:
        self.stats.last_error = message

    async def wait_for_budget(self) -> None:
        """Sleep until the portal's rate-limit pause (after a 429) is over.

        Background jobs call this so they wait out a pause instead of failing fast.
        """
        while (wait := self._throttle.paused_for) > 0:
            await asyncio.sleep(wait)

    # ------------------------------------------------------------------ endpoints

    async def search_meters(self, query: str = "", page: int = 1) -> dict[str, Any]:
        """One page (20 rows) of the portal's meter search. Matches meter id / serial."""
        path = "/portal/meters/search"
        body = _expect_ok(await self._get(path, params={"q": query, "page": page}))
        _rows(body, path)
        if not isinstance(body.get("total"), int):
            raise PortalProtocolError(f"unexpected payload shape from {path}: no integer 'total'")
        return body

    async def list_meter_ids(self) -> list[str]:
        """All meter ids, by walking the search listing (fallback when export is unavailable)."""
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            body = await self.search_meters(page=page)
            rows.extend(body["data"])
            if len(rows) >= body["total"]:  # re-read every page: the listing may change under us
                break
            if not body["data"]:
                raise PortalProtocolError(
                    f"/portal/meters/search returned an empty page {page} after {len(rows)} of {body['total']} rows"
                )
            page += 1
        return list(dict.fromkeys(str(row["meterId"]) for row in rows if row.get("meterId")))

    async def list_transformers(self) -> list[dict[str, Any]]:
        """Every distribution transformer (DT); the portal pages them 20 at a time."""
        path = "/portal/dts"
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            body = _expect_ok(await self._get(path, params={"page": page}))
            data = _rows(body, path)
            rows.extend(data)
            total = body.get("total")
            if not isinstance(total, int):
                raise PortalProtocolError(f"unexpected payload shape from {path}: no integer 'total'")
            if len(rows) >= total:
                return rows
            if not data:  # a truncated list would silently drop transformer names and ratings
                raise PortalProtocolError(f"{path} returned an empty page {page} after {len(rows)} of {total} rows")
            page += 1

    async def get_meter_page(self, meter_id: str) -> dict[str, Any]:
        """Server-rendered data of the meter detail page (nameplate + hierarchy strings)."""
        resp = await self._get(f"/meters/{_segment(meter_id)}/__data.json")
        body = _expect_ok(resp)
        nodes = body.get("nodes") if isinstance(body, dict) else None
        if body.get("type") != "data" or not isinstance(nodes, list) or not nodes:
            raise PortalProtocolError("unexpected __data.json payload shape")
        node = nodes[-1] or {}
        if not isinstance(node, dict):
            raise PortalProtocolError("unexpected __data.json node shape")
        if node.get("type") == "error":
            if node.get("status") == 404:
                raise PortalNotFound(f"meter {meter_id} not found")
            raise PortalProtocolError(f"meter page error: HTTP {node.get('status')}")
        try:
            page = unflatten(node["data"])
        except (DevalueError, KeyError) as exc:
            raise PortalProtocolError(f"cannot decode meter page for {meter_id}: {exc}") from exc
        if not isinstance(page, dict):
            raise PortalProtocolError(f"meter page for {meter_id} is not an object")
        return page

    async def get_meter_energy(
        self,
        meter_id: str,
        start: date | None = None,
        end: date | None = None,
        *,
        max_wait_s: float | None = None,
    ) -> list[dict[str, Any]]:
        """Register readings. Without dates the portal returns the last 7 days *of its data*.

        The portal only understands ``YYYY-MM-DD``; anything else is silently ignored.
        `max_wait_s` shortens the overall deadline (e.g. when a cached copy could be served).
        """
        params = {}
        if start:
            params["from"] = start.isoformat()
        if end:
            params["to"] = end.isoformat()
        path = f"/portal/meters/{_segment(meter_id)}/energy"
        resp = await self._get(path, params=params or None, max_wait_s=max_wait_s)
        return _rows(_expect_ok(resp, not_found=f"meter {meter_id} not found"), path)

    async def export_meters(self) -> list[dict[str, Any]]:
        """The signed bulk export: every meter with hierarchy and coordinates in one call.

        (The portal UI's "Export all meters" sends ``page=1``; the parameter is ignored and
        all meters are returned, but it is part of the signed string so we mirror it.)
        """
        query = "page=1"
        for refreshed_secret in (False, True):
            if refreshed_secret or self._secret is None:
                self._secret = await self._fetch_signing_secret()
            headers = functools.partial(self._export_headers, self._secret, query)
            resp = await self._get(f"{EXPORT_PATH}?{query}", headers_factory=headers)
            if resp.status_code == 401 and _error_code(resp) == "signature_invalid" and not refreshed_secret:
                log.warning("export signature rejected; refreshing signing secret")
                continue
            return _rows(_expect_ok(resp), EXPORT_PATH)
        raise PortalProtocolError("export signature rejected even with a fresh secret")

    def _export_headers(self, secret: str, query: str) -> dict[str, str]:
        # Called per attempt, so a retried request carries a fresh, skew-corrected timestamp.
        return signature_headers(secret, "GET", EXPORT_PATH, query, self._server_time())

    async def _fetch_signing_secret(self) -> str:
        data = _expect_ok(await self._get("/portal/keys")).get("data")
        secret = data.get("signingSecret") if isinstance(data, dict) else None
        if not isinstance(secret, str) or not secret:
            # Describe the shape only: this payload holds a credential.
            shape = sorted(data) if isinstance(data, dict) else type(data).__name__
            raise PortalProtocolError(f"unexpected /portal/keys payload (data: {shape})")
        return secret


# ---------------------------------------------------------------------- helpers


def _segment(value: str) -> str:
    return quote(value, safe="")


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise PortalProtocolError(
            f"expected JSON from {resp.request.url.path}, got HTTP {resp.status_code} "
            f"({resp.headers.get('content-type', 'no content type')})"
        ) from exc


def _rows(body: Any, path: str) -> list[dict[str, Any]]:
    """The `data` array of a portal JSON payload, checked to be a list of objects."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise PortalProtocolError(f"unexpected payload shape from {path}: 'data' is not a list of objects")
    return data


def _error_code(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


def _expect_ok(resp: httpx.Response, *, not_found: str | None = None) -> dict[str, Any]:
    if resp.status_code == 404 and not_found:
        raise PortalNotFound(not_found)
    if resp.status_code != 200:
        raise PortalProtocolError(f"unexpected HTTP {resp.status_code} from {resp.request.url.path}")
    body = _json(resp)
    if not isinstance(body, dict):
        raise PortalProtocolError(f"expected a JSON object from {resp.request.url.path}")
    return body


def _is_auth_failure(resp: httpx.Response) -> bool:
    if resp.status_code == 401:
        return _error_code(resp) == "unauthorized"
    if resp.status_code in (301, 302, 303, 307, 308):
        return resp.headers.get("location", "").startswith("/login")
    if resp.status_code == 200 and resp.request.url.path.endswith("/__data.json"):
        # SvelteKit's "soft" redirect: HTTP 200 with a redirect instruction in the body.
        try:
            body = resp.json()
        except ValueError:
            return False
        return (
            isinstance(body, dict)
            and body.get("type") == "redirect"
            and str(body.get("location", "")).startswith("/login")
        )
    return False


def _cookie_max_age(resp: httpx.Response) -> int | None:
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith(f"{SESSION_COOKIE}="):
            match = _MAX_AGE_RE.search(header)
            return int(match.group(1)) if match else None
    return None


def _action_failure_message(body: dict[str, Any]) -> str:
    """Form-action failures carry devalue data, e.g. {"email": ..., "error": "Invalid ..."}."""
    try:
        data = unflatten(json.loads(body.get("data", "null")))
    except (DevalueError, ValueError, TypeError):
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, str) and error:
        return f"login failed: {error[:200]}"  # the portal's own sentence, never an object it echoes
    status = body.get("status")
    return f"login failed (HTTP {status if isinstance(status, int) else '?'})"


__all__ = ["SESSION_COOKIE", "ClientStats", "PortalClient", "PortalError"]
