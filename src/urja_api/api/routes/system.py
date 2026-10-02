from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..deps import ServicesDep, reject_unknown_params, require_api_key
from ..errors import problems

router = APIRouter(tags=["system"])

SyncSource = Literal["export", "crawl"]


class Health(BaseModel):
    status: str = "ok"


class SyncRun(BaseModel):
    started_at: datetime
    finished_at: datetime | None
    status: Literal["running", "ok", "failed"]
    source: SyncSource | None = Field(description="`export` (signed bulk export) or `crawl` (detail-page fallback).")
    meter_count: int | None
    transformer_count: int | None
    warnings: list[str]
    error: str | None


class ReferenceDataStatus(BaseModel):
    last_success_at: datetime | None
    age_s: int | None = Field(description="Seconds since the last successful sync.")
    stale: bool = Field(
        description="True if no sync has succeeded yet, or the last successful one is older than two sync intervals."
    )
    source: SyncSource | None
    interval_s: int
    last_run: SyncRun | None = Field(description="The most recent sync attempt, successful or not.")


class WarmupStatus(BaseModel):
    state: Literal["idle", "running", "done", "aborted"] = Field(
        description="`aborted`: the pass stopped because the portal refused our login."
    )
    done: int
    total: int
    failed: int = Field(description="Meters whose refresh failed (a cached copy, if any, was kept).")
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: dict[str, int] = Field(default_factory=dict, description="Failed fetches by error type.")


class ReadingsCacheStatus(BaseModel):
    meter_count: int = Field(description="Meters whose readings are cached.")
    meters_total: int = Field(description="Meters in the index (what a complete cache would hold).")
    oldest_fetch: datetime | None
    newest_fetch: datetime | None
    first_reading_at: datetime | None
    last_reading_at: datetime | None = Field(description="Latest reading held for any meter.")
    ttl_s: int
    warmup: WarmupStatus


class PortalClientStatus(BaseModel):
    base_url: str
    logins: int
    requests: int
    retries: int
    rate_limited: int = Field(description="429 responses received from the portal.")
    upstream_errors: int = Field(description="Network errors and 5xx responses (before retries).")
    last_error: str | None
    last_success_at: datetime | None
    session_expires_at: datetime | None
    clock_skew_s: float = Field(description="Portal clock minus ours, from its Date header (used for signing).")


class Status(BaseModel):
    index_ready: bool = Field(description="True once reference data has been synced at least once.")
    reference_data: ReferenceDataStatus
    readings_cache: ReadingsCacheStatus
    portal: PortalClientStatus


class SyncResult(BaseModel):
    source: SyncSource
    meter_count: int
    transformer_count: int = Field(description="Transformers served: those with at least one meter.")
    warnings: list[str]


@router.get("/healthz", response_model=Health, summary="Liveness probe")
async def healthz() -> Health:
    return Health()


@router.get(
    "/v1/status",
    response_model=Status,
    responses=problems(401, 422),
    summary="Sync freshness and portal client health",
    dependencies=[Depends(require_api_key), Depends(reject_unknown_params)],
)
def status(services: ServicesDep) -> Status:
    reference = services.sync.reference_status()
    last_run = reference.pop("last_run")
    cache = services.store.readings_fetch_summary()
    stats = services.client.stats
    return Status(
        index_ready=services.store.has_reference_data(),
        reference_data=ReferenceDataStatus(
            **reference,
            last_run=_sync_run(last_run) if last_run else None,
        ),
        readings_cache=ReadingsCacheStatus(
            meter_count=cache["meters"],
            meters_total=len(services.store.meter_ids()),
            oldest_fetch=cache["oldest"],
            newest_fetch=cache["newest"],
            first_reading_at=cache["first_ts"],
            last_reading_at=cache["last_ts"],
            ttl_s=int(services.sync.readings_ttl.total_seconds()),
            warmup=WarmupStatus(**services.sync.warmup),
        ),
        portal=PortalClientStatus(
            base_url=services.client.base_url,
            logins=stats.logins,
            requests=stats.requests,
            retries=stats.retries,
            rate_limited=stats.rate_limited,
            upstream_errors=stats.upstream_errors,
            last_error=stats.last_error,
            last_success_at=_from_epoch(stats.last_success_at),
            session_expires_at=_from_epoch(stats.session_expires_at),
            clock_skew_s=stats.clock_skew_s,
        ),
    )


@router.post(
    "/v1/sync",
    response_model=SyncResult,
    responses=problems(401, 422, 502, 503),
    summary="Re-sync reference data from the portal now",
    dependencies=[Depends(require_api_key), Depends(reject_unknown_params)],
)
async def trigger_sync(services: ServicesDep) -> SyncResult:
    """Joins a sync that is already running, and returns the last result instead of
    syncing again if one succeeded less than 30 s ago, so repeated calls can't spend the
    portal's rate budget."""
    summary = await services.sync.request_sync()
    return SyncResult(
        source=summary["source"],
        meter_count=summary["meters"],
        transformer_count=summary["transformers"],
        warnings=summary["warnings"],
    )


def _sync_run(row: dict[str, Any]) -> SyncRun:
    return SyncRun(
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        source=row["source"],
        meter_count=row["meters"],
        transformer_count=row["transformers"],
        warnings=json.loads(row["warnings"]),
        error=row["error"],
    )


def _from_epoch(epoch: float | None) -> datetime | None:
    return datetime.fromtimestamp(epoch, UTC) if epoch else None
