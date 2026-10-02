from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings, get_settings
from ..derived import DerivedViews
from ..portal.client import PortalClient
from ..store import Store
from ..sync import SyncService
from .deps import Services, reject_unknown_params, require_api_key
from .errors import install_error_handlers, problem_media_types
from .routes import insights, meters, network, system, transformers

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

DESCRIPTION = """
A clean, read-only REST API over the **Urja Meter Ops** portal (smart-meter data for a
Jaipur distribution utility). The portal has no API of its own. This service logs in as a
normal user, reads the portal's internal endpoints (see `PROTOCOL.md` in the repository)
and serves the data as typed JSON.

* **Meters**: nameplate, status, location, network position; filter by any attribute,
  network node or distance.
* **Readings / consumption**: register readings normalised to numbers and ISO timestamps
  (IST), with per-interval and per-day consumption derived from register deltas.
* **Network**: the zone → circle → division → subdivision → substation → feeder →
  transformer hierarchy, reconstructed and repaired from per-meter data.
* **Data quality**: every value we repaired or could not reconcile, with the portal's
  original value.

Reference data is served from a local index synced from the portal's bulk export;
readings are fetched on demand and cached. `GET /v1/status` shows how fresh everything is.
Errors use `application/problem+json` (RFC 9457) with a stable `code` (listed under the
`Problem` schema). Unknown query parameters are rejected rather than ignored.
"""

TAGS = [
    {"name": "meters", "description": "Meter records, readings and consumption."},
    {"name": "transformers", "description": "Distribution transformers (DTs) and the meters on them."},
    {"name": "network", "description": "The reconstructed distribution network."},
    {"name": "data quality", "description": "Corrections applied to the portal's data."},
    {"name": "insights", "description": "Fleet-wide analysis over the local cache: anomalies, consumption roll-ups."},
    {"name": "system", "description": "Health, freshness and sync control."},
]


def create_app(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    background_sync: bool = True,
) -> FastAPI:
    """Build the app. `transport` lets tests plug in a fake portal; settings load lazily so
    the OpenAPI document can be generated without credentials."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        client = PortalClient(
            cfg.portal_base_url,
            cfg.portal_email,
            cfg.portal_password.get_secret_value(),
            timeout_s=cfg.portal_timeout_s,
            rate_limit_per_minute=cfg.portal_rate_limit_per_minute,
            rate_limit_burst=cfg.portal_rate_limit_burst,
            max_wait_s=cfg.portal_max_wait_s,
            transport=transport,
        )
        store = Store(cfg.db_path)
        sync = SyncService(
            client,
            store,
            sync_interval_s=cfg.sync_interval_s,
            readings_ttl_s=cfg.readings_ttl_s,
            warm_readings=cfg.warm_readings,
            warm_interval_s=cfg.warm_interval_s,
        )
        app.state.services = Services(settings=cfg, client=client, store=store, sync=sync, derived=DerivedViews(store))
        if background_sync:
            sync.start()
        try:
            yield
        finally:
            await sync.stop()
            await client.aclose()
            store.close()

    app = FastAPI(
        title="Urja Meter API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS,
        lifespan=lifespan,
        generate_unique_id_function=lambda route: route.name,  # operationId = the handler's name
    )
    install_error_handlers(app)
    app.include_router(system.router)
    for router in (meters.router, transformers.router, network.router, insights.router):
        app.include_router(router, dependencies=[Depends(require_api_key), Depends(reject_unknown_params)])
    _customise_openapi(app)

    if WEB_DIR.is_dir():
        app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="web")

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse("/app/")

    return app


def _customise_openapi(app: FastAPI) -> None:
    """Two corrections FastAPI can't express directly: error bodies are
    `application/problem+json`, and the API key is optional (only enforced when the
    server sets URJA_API_KEY)."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = generate()
            problem_media_types(schema)
            for operations in schema.get("paths", {}).values():
                for operation in operations.values():
                    if operation.get("security"):
                        operation["security"] = [{}, *operation["security"]]
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = openapi  # type: ignore[method-assign]
