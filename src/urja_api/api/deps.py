from __future__ import annotations

import difflib
import hmac
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.security import APIKeyHeader

from ..config import Settings
from ..derived import DerivedViews
from ..portal.client import PortalClient
from ..store import Store
from ..sync import SyncService
from .errors import ApiProblem


@dataclass
class Services:
    settings: Settings
    client: PortalClient
    store: Store
    sync: SyncService
    derived: DerivedViews

    def now(self) -> datetime:
        """The one clock the service judges time by (tests pin it on the sync service)."""
        return self.sync.clock()


def get_services(request: Request) -> Services:
    return request.app.state.services


_api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="Required only if the server is configured with URJA_API_KEY."
)


def require_api_key(
    services: Annotated[Services, Depends(get_services)],
    api_key: Annotated[str | None, Security(_api_key_header)] = None,
) -> None:
    expected = services.settings.api_key
    if expected is None:
        return
    # Compare bytes: compare_digest refuses non-ASCII strings. Header values arrive decoded
    # as latin-1, so encoding them back gives exactly the bytes the client sent (the
    # configured key is ASCII: see Settings).
    given = api_key.encode("latin-1") if api_key is not None else None
    if given is None or not hmac.compare_digest(given, expected.get_secret_value().encode()):
        raise ApiProblem(401, "unauthorized", "A valid X-API-Key header is required.")


def reject_unknown_params(request: Request) -> None:
    """Unknown query parameters are an error rather than silently ignored: a misspelt
    filter (say `transformer_code=` for `transformer=`) would otherwise match everything."""
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return
    known = _query_names(route.dependant)
    unknown = sorted(set(request.query_params) - known)
    if not unknown:
        return
    errors = []
    for name in unknown:
        message = "unknown query parameter"
        if close := difflib.get_close_matches(name, sorted(known), n=1):
            message += f"; did you mean {close[0]!r}?"
        errors.append({"location": ["query", name], "message": message, "type": "unknown_parameter"})
    raise ApiProblem(422, "validation_error", f"Unknown query parameter(s): {', '.join(unknown)}.", errors=errors)


def _query_names(dependant: Dependant) -> set[str]:
    names = {param.alias for param in dependant.query_params}
    for sub in dependant.dependencies:
        names |= _query_names(sub)
    return names


def require_index(services: Annotated[Services, Depends(get_services)]) -> Services:
    """Reference data must have been synced at least once before we can answer."""
    if services.store.has_reference_data():
        return services
    runs = services.store.last_sync_runs(1)
    last = runs[0] if runs else None
    if last is not None and last["status"] == "failed":
        detail = (
            f"The first sync with the portal failed ({last['error']}). It is retried automatically; "
            "GET /v1/status shows the details."
        )
        retry_after = services.sync.seconds_to_next_sync() or 5
    else:
        detail = "The local index is still being built from the portal; try again shortly."
        retry_after = 5
    raise ApiProblem(503, "index_not_ready", detail, headers={"Retry-After": str(retry_after)})


ServicesDep = Annotated[Services, Depends(get_services)]
IndexedServices = Annotated[Services, Depends(require_index)]
