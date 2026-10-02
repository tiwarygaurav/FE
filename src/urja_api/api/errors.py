"""Errors as RFC 9457 `application/problem+json`, including upstream (portal) failures.

Every error carries a stable `code`. `ERROR_CODES` is the complete list; it is rendered
into the OpenAPI description of `Problem.code`, and the README's error table mirrors it.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..portal.errors import (
    PortalAuthError,
    PortalNotFound,
    PortalProtocolError,
    PortalRateLimited,
    PortalUnavailable,
    RequestQueueFull,
)
from ..sync import SyncRejected

log = logging.getLogger(__name__)
PROBLEM_JSON = "application/problem+json"

# code -> (HTTP status, when it is returned)
ERROR_CODES: dict[str, tuple[str, str]] = {
    "unauthorized": ("401", "`X-API-Key` is missing or wrong (only when the server sets `URJA_API_KEY`)."),
    "meter_not_found": ("404", "No meter with that id in the index, or the portal no longer has it."),
    "transformer_not_found": ("404", "No distribution transformer with that code."),
    "network_node_not_found": ("404", "No network node with that level and code."),
    "not_found": ("404", "No such path."),
    "method_not_allowed": ("405", "The path exists, but not for this HTTP method."),
    "http_error": ("4xx", "Any other HTTP-level error."),
    "validation_error": ("422", "A parameter is malformed, out of range or not recognised; `errors` says which."),
    "granularity_unavailable": ("422", "Hourly consumption asked of a meter that reports once a day."),
    "internal_error": ("500", "A bug in this service; the server log has the details."),
    "upstream_auth_failed": ("502", "This service could not log in to the portal."),
    "upstream_protocol_error": ("502", "The portal answered in a format this service does not understand."),
    "sync_rejected": ("502", "A sync returned data that looked broken; the previous snapshot is kept."),
    "index_not_ready": ("503", "The first sync with the portal hasn't finished; `Retry-After` gives the next attempt."),
    "upstream_unavailable": ("503", "The portal is down or too slow, and there is no cached copy to serve."),
    "upstream_rate_limited": ("503", "The portal kept answering 429, and there is no cached copy to serve."),
    "busy": ("503", "Too many requests are queued for the portal's rate budget."),
}


class FieldError(BaseModel):
    location: list[str | int] = Field(description='Where the value came from, e.g. `["query", "from"]`.')
    message: str
    type: str = Field(description="Machine-readable kind, e.g. `value_error` or `unknown_parameter`.")


class Problem(BaseModel):
    """RFC 9457 problem details."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    code: str = Field(
        description="Stable machine-readable error code:\n\n"
        + "\n".join(f"* `{code}` ({status}): {when}" for code, (status, when) in ERROR_CODES.items()),
        examples=["meter_not_found"],
    )
    errors: list[FieldError] | None = Field(default=None, description="Field-level validation errors.")


class ApiProblem(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        detail: str,
        *,
        headers: dict[str, str] | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers or {}
        self.errors = errors


_TITLES = {
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    405: "Method Not Allowed",
    422: "Unprocessable Content",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}
_RETRY_AFTER = {"Retry-After": {"description": "Seconds to wait before retrying.", "schema": {"type": "integer"}}}


def problems(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """OpenAPI `responses` for the error statuses an operation can return.

    The schema lands under `application/json`; `problem_media_types` moves it to
    `application/problem+json`, the type actually sent, once the document is built.
    """
    responses: dict[int | str, dict[str, Any]] = {}
    for status in statuses:
        entry: dict[str, Any] = {"model": Problem, "description": _TITLES[status]}
        if status == 503:
            entry["headers"] = _RETRY_AFTER
        responses[status] = entry
    return responses


def problem_media_types(openapi: dict[str, Any]) -> None:
    """Declare every 4xx/5xx body as `application/problem+json` in a generated document."""
    for operations in openapi.get("paths", {}).values():
        for operation in operations.values():
            for status, response in operation.get("responses", {}).items():
                content = response.get("content", {})
                if status[0] in "45" and "application/json" in content:
                    content[PROBLEM_JSON] = content.pop("application/json")


def problem_response(problem: ApiProblem) -> JSONResponse:
    body = Problem(
        title=_TITLES.get(problem.status, "Error"),
        status=problem.status,
        detail=problem.detail,
        code=problem.code,
        errors=problem.errors,
    )
    return JSONResponse(
        body.model_dump(exclude_none=True), status_code=problem.status, headers=problem.headers, media_type=PROBLEM_JSON
    )


def _seconds(value: float) -> str:
    return str(max(1, math.ceil(value)))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def _api_problem(_: Request, exc: ApiProblem) -> JSONResponse:
        return problem_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"location": list(e.get("loc", ())), "message": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]
        return problem_response(ApiProblem(422, "validation_error", "The request is invalid.", errors=errors))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        headers = dict(exc.headers) if exc.headers else None  # e.g. Allow on a 405
        if exc.status_code == 405 and not headers:  # the static mount raises its 405 without one
            headers = {"Allow": "GET, HEAD"}
        return problem_response(ApiProblem(exc.status_code, code, str(exc.detail), headers=headers))

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Starlette re-raises the exception after this response is sent, so the server
        # logs the traceback; logging it here as well would only duplicate it.
        return problem_response(ApiProblem(500, "internal_error", "Something went wrong in this service."))

    # --- the portal misbehaving, in terms a consumer can act on -----------------------

    @app.exception_handler(PortalNotFound)
    async def _portal_not_found(_: Request, exc: PortalNotFound) -> JSONResponse:
        # Only meter endpoints reach the portal: the index has the meter, the portal doesn't.
        return problem_response(
            ApiProblem(
                404, "meter_not_found", f"The portal no longer has this meter ({exc}); it may have been removed."
            )
        )

    @app.exception_handler(PortalRateLimited)
    async def _portal_rate_limited(_: Request, exc: PortalRateLimited) -> JSONResponse:
        return problem_response(
            ApiProblem(
                503,
                "upstream_rate_limited",
                "The portal's rate limit is exhausted and no cached copy is available; retry later.",
                headers={"Retry-After": _seconds(exc.retry_after)},
            )
        )

    @app.exception_handler(RequestQueueFull)
    async def _busy(_: Request, exc: RequestQueueFull) -> JSONResponse:
        return problem_response(
            ApiProblem(
                503,
                "busy",
                "Too many requests are waiting for the portal's rate budget; retry later.",
                headers={"Retry-After": _seconds(exc.retry_after)},
            )
        )

    @app.exception_handler(PortalUnavailable)
    async def _portal_unavailable(_: Request, exc: PortalUnavailable) -> JSONResponse:
        log.warning("portal unavailable: %s", exc)
        return problem_response(
            ApiProblem(
                503, "upstream_unavailable", "The portal is not responding; retry later.", headers={"Retry-After": "30"}
            )
        )

    @app.exception_handler(PortalAuthError)
    async def _portal_auth(_: Request, exc: PortalAuthError) -> JSONResponse:
        log.error("portal login failed: %s", exc)
        return problem_response(ApiProblem(502, "upstream_auth_failed", "This service could not log in to the portal."))

    @app.exception_handler(SyncRejected)
    async def _sync_rejected(_: Request, exc: SyncRejected) -> JSONResponse:
        log.warning("sync rejected: %s", exc)
        return problem_response(ApiProblem(502, "sync_rejected", str(exc)))

    @app.exception_handler(PortalProtocolError)
    async def _portal_protocol(_: Request, exc: PortalProtocolError) -> JSONResponse:
        log.error("portal protocol error: %s", exc)
        return problem_response(
            ApiProblem(502, "upstream_protocol_error", "The portal answered in an unexpected format.")
        )
