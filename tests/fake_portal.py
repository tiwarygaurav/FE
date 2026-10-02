"""An in-process imitation of the Urja Meter Ops portal, quirks included.

Tests run the real `PortalClient` against this app through `httpx.ASGITransport`, so
session expiry, soft redirects, rate limiting and signature checks are exercised for real
rather than mocked call by call. The behaviour here was observed on the live portal
(see PROTOCOL.md): one rate budget shared by every /portal/* call, 429s not charged
against it, signature timestamps accepted up to ±240 s off (the largest offset that
passed; 299 s failed). One detail is inferred rather than observed: `_` as a search
wildcard, by analogy with `%`. It only serves the routes this service uses. Knobs on
`FakePortal` let tests inject failures.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

COOKIE = "__Secure-better-auth.session_token"
EMAIL = "tester@portal.test"  # fake credentials: the real ones never enter the repository
PASSWORD = "fake-portal-password"
BASE_URL = "https://portal.test"
PAGE_SIZE = 20
_LEVELS = [
    ("Zone", "zone"),
    ("Circle", "circle"),
    ("Division", "division"),
    ("Subdivision", "subdivision"),
    ("Sub Station", "substation"),
    ("Feeder", "feeder"),
    ("DT", "dt"),
]


def devalue_flatten(value: Any) -> list[Any]:
    """Minimal devalue encoder (objects, arrays, primitives; shared primitives de-duplicated)."""
    values: list[Any] = []
    seen: dict[Any, int] = {}

    def walk(v: Any) -> int:
        key = ("o", id(v)) if isinstance(v, dict | list) else ("p", type(v).__name__, v)
        if key in seen:
            return seen[key]
        index = len(values)
        seen[key] = index
        values.append(None)
        if isinstance(v, dict):
            values[index] = {k: walk(x) for k, x in v.items()}
        elif isinstance(v, list):
            values[index] = [walk(x) for x in v]
        else:
            values[index] = v
        return index

    walk(value)
    return values


def _parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%d/%m/%Y %H:%M")


@dataclass
class FakePortal:
    meters: list[dict[str, Any]]  # records in the export format
    transformers: list[dict[str, Any]]  # records in the /portal/dts format
    energy: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    session_ttl_s: int = 3600
    rate_limit: int = 120
    rate_window_s: float = 60.0
    signature_tolerance_s: int = 240
    password: str = PASSWORD  # change it to make the portal refuse our login
    clock: Any = time.time

    # --- state & fault injection -------------------------------------------------
    sessions: dict[str, float] = field(default_factory=dict)  # token -> expiry epoch
    signing_secret: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    fail_next: list[int] = field(default_factory=list)  # status codes to return next (5xx)
    fail_paths: dict[str, int] = field(default_factory=dict)  # path -> status returned on every call
    calls: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int))
    _window_start: float | None = None
    _window_count: int = 0

    def __post_init__(self) -> None:
        self._by_id = {m["meterId"]: m for m in self.meters}
        self.app = Starlette(
            routes=[
                Route("/login", self.login, methods=["POST"]),
                Route("/api/auth/sign-out", self.sign_out, methods=["POST"]),
                Route("/portal/meters/search", self.search),
                Route("/portal/dts", self.dts),
                Route("/portal/keys", self.keys),
                Route("/portal/export", self.export),
                Route("/portal/meters/{meter_id}/energy", self.energy_series),
                Route("/meters/{meter_id}/__data.json", self.meter_page),
            ]
        )
        self.app.add_middleware(_FaultInjection, portal=self)

    # --- helpers --------------------------------------------------------------------

    def expire_all_sessions(self) -> None:
        self.sessions.clear()

    def remove_meter(self, meter_id: str) -> None:
        """The portal forgets a meter: it leaves the export and its pages answer 404."""
        self.meters = [m for m in self.meters if m["meterId"] != meter_id]
        self._by_id.pop(meter_id, None)

    def _session_ok(self, request: Request) -> bool:
        token = (request.cookies.get(COOKIE) or "").split(".")[0]
        expiry = self.sessions.get(token)
        return expiry is not None and expiry > self.clock()

    @staticmethod
    def _unauthorized() -> JSONResponse:
        return JSONResponse({"error": "unauthorized", "message": "A valid session is required."}, 401)

    @staticmethod
    def _not_found() -> JSONResponse:
        return JSONResponse({"error": "not_found", "message": "Meter not found"}, 404)

    def rate_limited(self) -> bool:
        """`rate_limit` calls per window, the window opening with the first call after the
        previous one expired. A refused call is not charged."""
        now = self.clock()
        if self._window_start is None or now - self._window_start >= self.rate_window_s:
            self._window_start, self._window_count = now, 0
        if self._window_count >= self.rate_limit:
            return True
        self._window_count += 1
        return False

    @staticmethod
    def _page(raw: str | None) -> int:
        try:
            return max(1, int(raw or "1"))
        except ValueError:
            return 1

    # --- auth -----------------------------------------------------------------------

    async def login(self, request: Request) -> Response:
        if request.headers.get("origin") != BASE_URL:
            return JSONResponse({"message": "Cross-site POST form submissions are forbidden"}, 403)
        # Parsed by hand: Starlette's request.form() would need python-multipart.
        form = dict(parse_qsl((await request.body()).decode()))
        if form.get("email") != EMAIL or form.get("password") != self.password:
            data = json.dumps(devalue_flatten({"email": form.get("email"), "error": "Invalid email or password."}))
            return JSONResponse({"type": "failure", "status": 401, "data": data})
        token = secrets.token_urlsafe(24)
        self.sessions[token] = self.clock() + self.session_ttl_s
        resp = JSONResponse({"type": "redirect", "status": 303, "location": "/meters"})
        resp.headers.append(
            "set-cookie",
            f"{COOKIE}={token}.sig; Max-Age={self.session_ttl_s}; Path=/; HttpOnly; Secure; SameSite=Lax",
        )
        return resp

    async def sign_out(self, request: Request) -> Response:
        token = (request.cookies.get(COOKIE) or "").split(".")[0]
        self.sessions.pop(token, None)
        return JSONResponse({"success": True})

    # --- JSON endpoints ------------------------------------------------------------------

    async def search(self, request: Request) -> Response:
        if not self._session_ok(request):
            return self._unauthorized()
        q = request.query_params.get("q", "")
        # The portal passes q into a SQL LIKE unescaped: '%' acts as a wildcard (observed), so '_' should too.
        pattern = re.compile("".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in q), re.IGNORECASE)
        rows = [
            {k: m[k] for k in ("meterId", "serialNo", "make", "phaseType", "installStatus", "dtCode")}
            for m in self.meters
            if pattern.search(m["meterId"]) or pattern.search(m["serialNo"])
        ]
        page = self._page(request.query_params.get("page"))
        start = (page - 1) * PAGE_SIZE
        return JSONResponse(
            {"data": rows[start : start + PAGE_SIZE], "total": len(rows), "page": page, "pageSize": PAGE_SIZE}
        )

    async def dts(self, request: Request) -> Response:
        if not self._session_ok(request):
            return self._unauthorized()
        page = self._page(request.query_params.get("page"))
        start = (page - 1) * PAGE_SIZE
        return JSONResponse(
            {
                "data": self.transformers[start : start + PAGE_SIZE],
                "total": len(self.transformers),
                "page": page,
                "pageSize": PAGE_SIZE,
            }
        )

    async def keys(self, request: Request) -> Response:
        if not self._session_ok(request):
            return self._unauthorized()
        return JSONResponse({"data": {"signingSecret": self.signing_secret}})

    async def export(self, request: Request) -> Response:
        if not self._session_ok(request):
            return self._unauthorized()
        ts = request.headers.get("x-timestamp", "")
        sig = request.headers.get("x-signature", "")
        query = request.url.query
        expected = hmac.new(
            self.signing_secret.encode(), f"GET\n/portal/export\n{query}\n{ts}".encode(), hashlib.sha256
        ).hexdigest()
        fresh = ts.isdigit() and abs(int(ts) - self.clock()) <= self.signature_tolerance_s
        if not (fresh and hmac.compare_digest(sig, expected)):
            return JSONResponse({"error": "signature_invalid", "message": "Missing or invalid request signature."}, 401)
        return JSONResponse({"data": self.meters, "total": len(self.meters)})  # paging is ignored

    async def energy_series(self, request: Request) -> Response:
        if not self._session_ok(request):
            return self._unauthorized()
        meter_id = request.path_params["meter_id"]
        if meter_id not in self._by_id:
            return self._not_found()
        rows = self.energy.get(meter_id, [])
        if not rows:
            return JSONResponse({"data": []})

        def parse_day(raw: str | None) -> date | None:  # anything but YYYY-MM-DD is ignored
            try:
                return date.fromisoformat(raw) if raw and len(raw) == 10 else None
            except ValueError:
                return None

        start_day = parse_day(request.query_params.get("from"))
        end_day = parse_day(request.query_params.get("to"))
        stamps = [_parse_ts(r["timestamp"]) for r in rows]
        if start_day is None and end_day is None:
            end = max(stamps)
            start = end - timedelta(days=7)
        elif end_day is None:
            start = datetime.combine(start_day, datetime.min.time())
            end = start + timedelta(days=7)
        else:
            end = datetime.combine(end_day, datetime.max.time())
            start = (
                datetime.combine(start_day, datetime.min.time())
                if start_day
                else datetime.combine(end_day - timedelta(days=7), datetime.min.time())
            )
        return JSONResponse({"data": [r for r, t in zip(rows, stamps, strict=True) if start <= t <= end]})

    # --- SvelteKit page data -----------------------------------------------------------

    async def meter_page(self, request: Request) -> Response:
        if not self._session_ok(request):
            return JSONResponse({"type": "redirect", "location": "/login"})  # HTTP 200 "soft" redirect
        layout = {"type": "data", "data": devalue_flatten({"user": {"name": "Ops Desk", "email": EMAIL}}), "uses": {}}
        meter = self._by_id.get(request.path_params["meter_id"])
        if meter is None:
            page_node = {"type": "error", "error": {"message": "Meter not found"}, "status": 404}
            return JSONResponse({"type": "data", "nodes": [None, layout, page_node]})

        if meter.get("build") == "v2":
            detail: dict[str, Any] = {
                "classData": json.dumps(
                    {
                        "installed_meter": {
                            "MeterId": meter["meterId"],
                            "SerialNo": meter["serialNo"],
                            "Make": meter["make"],
                            "PhaseType": meter["phaseType"],
                            "InstallationStatus": meter["installStatus"],
                            "InstallationType": meter["installType"],
                        }
                    }
                )
            }
        else:
            params = [
                ("Meter ID", meter["meterId"]),
                ("Serial No", meter["serialNo"]),
                ("Make", meter["make"]),
                ("Phase Type", meter["phaseType"]),
                ("Installation Status", meter["installStatus"]),
                ("Installation Type", meter["installType"]),
            ]
            detail = {"data": [{"parameterName": n, "parameterValue": v} for n, v in params]}
        hierarchy: dict[str, str] = {
            "Meter ID": meter["meterId"],
            "Installation Status": meter["installStatus"],
            "Installation Type": meter["installType"],
        }
        for label, key in _LEVELS:
            node = meter["hierarchy"][key]
            # The portal renders `name && code ? "name (code)" : name || ""`: a blank name loses the code.
            hierarchy[label] = f"{node['name']} ({node['code']})" if node["name"] and node["code"] else node["name"]
        page = {"meterId": meter["meterId"], "detail": detail, "hierarchy": hierarchy}
        page_node = {"type": "data", "data": devalue_flatten(page), "uses": {"params": ["id"]}}
        return JSONResponse({"type": "data", "nodes": [None, layout, page_node]})


class _FaultInjection:
    """ASGI middleware: count calls, inject queued or per-path failures, and apply the
    rate budget that every /portal/* call shares."""

    def __init__(self, app: Any, portal: FakePortal) -> None:
        self.app = app
        self.portal = portal

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            # Yield like a network round trip would. Without it an in-process request never
            # suspends, so "concurrent" requests run one after another and locks go untested.
            await asyncio.sleep(0)
            path = scope["path"]
            self.portal.calls[path] += 1
            status = self.portal.fail_next.pop(0) if self.portal.fail_next else self.portal.fail_paths.get(path)
            if status:
                await Response("upstream exploded", status_code=status)(scope, receive, send)
                return
            if path.startswith("/portal/") and self.portal.rate_limited():
                body = {"error": "rate_limited", "message": "Rate limit exceeded; slow down."}
                await JSONResponse(body, 429)(scope, receive, send)
                return
        await self.app(scope, receive, send)
