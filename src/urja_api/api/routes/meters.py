from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Query

from ...derived import detect_for_meter
from ...domain.models import (
    ConsumptionResponse,
    Freshness,
    Granularity,
    InstallationType,
    Meter,
    MeterAnomalies,
    MeterListItem,
    MeterStatus,
    NetworkLevel,
    Page,
    Phase,
    ReadingsResponse,
)
from ...domain.readings import (
    StoredReading,
    bucket_floor,
    build_readings,
    consumption_buckets,
    detect_interval_minutes,
    register_span,
)
from ...store import SORT_ALIASES, SORT_COLUMNS, MeterFilter
from ..deps import IndexedServices, Services
from ..errors import ApiProblem, problems
from ..views import (
    anomalies_out,
    meter_detail,
    meter_list_item,
    normalize_meter_id,
    parse_bbox,
    parse_point,
    parse_window,
    resolve_window,
)

router = APIRouter(prefix="/v1/meters", tags=["meters"])

SORT_KEYS = (*SORT_COLUMNS, "distance")

MeterId = Annotated[
    str, Path(description="The meter's id as the portal shows it (any case accepted).", examples=["J100000"])
]
FromParam = Annotated[
    str | None,
    Query(
        alias="from",
        description="Window start: `YYYY-MM-DD` or an ISO 8601 datetime between 2000 and 2100. Datetimes are "
        "converted to IST; naive ones are read as IST. Defaults to 7 days before `to`.",
        examples=["2026-06-01"],
    ),
]
ToParam = Annotated[
    str | None,
    Query(
        alias="to",
        description="Window end, inclusive; a bare date covers the whole day. "
        "Defaults to the meter's latest reading, which may be long before today: compare `last_reading_at` "
        "with the current time.",
        examples=["2026-06-07"],
    ),
]


@router.get("", response_model=Page[MeterListItem], responses=problems(401, 422, 503), summary="List and filter meters")
def list_meters(
    services: IndexedServices,
    q: Annotated[str | None, Query(description="Case-insensitive substring of meter id or serial number.")] = None,
    status: Annotated[
        list[MeterStatus] | None, Query(description="Repeatable, e.g. `status=faulty&status=installed`.")
    ] = None,
    make: Annotated[list[str] | None, Query(description="Repeatable, case-insensitive, e.g. `make=HPL`.")] = None,
    phase: Annotated[Phase | None, Query(description="Single- or three-phase meters only.")] = None,
    installation_type: Annotated[
        InstallationType | None, Query(description="Whole-current or CT-operated meters only.")
    ] = None,
    zone: Annotated[str | None, Query(description="Zone code, e.g. `Z-02`.")] = None,
    circle: Annotated[str | None, Query(description="Circle code.")] = None,
    division: Annotated[str | None, Query(description="Division code.")] = None,
    subdivision: Annotated[str | None, Query(description="Subdivision code.")] = None,
    substation: Annotated[str | None, Query(description="Substation code.")] = None,
    feeder: Annotated[str | None, Query(description="Feeder code, e.g. `F-001` (the meters' `feeder_code`).")] = None,
    transformer: Annotated[
        str | None, Query(description="Distribution transformer code, e.g. `DT-001` (the meters' `transformer_code`).")
    ] = None,
    has_issues: Annotated[bool | None, Query(description="Only meters with (or without) data-quality issues.")] = None,
    interval_minutes: Annotated[
        int | None,
        Query(
            description="Reading interval: 30 (half-hourly) or 1440 (daily). Only meters whose readings are "
            "cached can match (`/v1/status` shows how many are)."
        ),
    ] = None,
    near: Annotated[
        str | None,
        Query(
            description="`lat,lng`: return meters within `radius_km`, nearest first, with `distance_km`.",
            examples=["26.9124,75.7873"],
        ),
    ] = None,
    radius_km: Annotated[float, Query(gt=0, le=50, description="Search radius around `near`, in km.")] = 1.0,
    bbox: Annotated[
        str | None,
        Query(
            description="`min_lng,min_lat,max_lng,max_lat` (GeoJSON order). With `near=`, a meter must be in both.",
            examples=["75.78,26.90,75.82,26.94"],
        ),
    ] = None,
    sort: Annotated[
        str | None,
        Query(
            pattern=rf"^-?({'|'.join([*SORT_KEYS, *SORT_ALIASES])})$",
            description="Sort key, prefixed with `-` for descending: "
            + ", ".join(f"`{key}`" for key in SORT_KEYS)
            + " (`distance` needs `near=`). The response's field names (`transformer_code`, `data_issue_count`, "
            "...) work too. Defaults to `distance` with `near=`, else `meter_id`; ties are broken by `meter_id`.",
            examples=["-issues"],
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500, description="Page size.")] = 50,
    offset: Annotated[int, Query(ge=0, description="Items to skip.")] = 0,
) -> Page[MeterListItem]:
    levels = {
        NetworkLevel.zone: zone,
        NetworkLevel.circle: circle,
        NetworkLevel.division: division,
        NetworkLevel.subdivision: subdivision,
        NetworkLevel.substation: substation,
        NetworkLevel.feeder: feeder,
        NetworkLevel.transformer: transformer,
    }
    sort_key = SORT_ALIASES.get((sort or "").lstrip("-"), (sort or "").lstrip("-"))
    if sort_key == "distance" and not near:
        raise ApiProblem(
            422,
            "validation_error",
            "Sorting by distance needs a `near=` point.",
            errors=[{"location": ["query", "sort"], "message": "requires near", "type": "value_error"}],
        )
    filters = MeterFilter(
        q=q,
        status=[s.value for s in status or []],
        make=make or [],
        phase=phase.value if phase else None,
        installation_type=installation_type.value if installation_type else None,
        network={level.value: code.strip().upper() for level, code in levels.items() if code},
        has_issues=has_issues,
        interval_minutes=interval_minutes,
        near=parse_point(near, "near"),
        radius_km=radius_km,
        bbox=parse_bbox(bbox),
        sort=sort,
    )
    rows, total = services.store.search_meters(filters, limit, offset)
    return Page[MeterListItem](
        items=[meter_list_item(row, distance) for row, distance in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{meter_id}", response_model=Meter, responses=problems(401, 404, 422, 503), summary="Get one meter")
def get_meter(meter_id: MeterId, services: IndexedServices) -> Meter:
    row = _meter_row(services, meter_id)
    return meter_detail(row, services.store.get_transformer(row["transformer_code"]))


@router.get(
    "/{meter_id}/readings",
    response_model=ReadingsResponse,
    responses=problems(401, 404, 422, 502, 503),
    summary="Register readings with per-interval consumption",
)
async def get_readings(
    meter_id: MeterId, services: IndexedServices, start: FromParam = None, end: ToParam = None
) -> ReadingsResponse:
    """Normalised register readings (numbers, ISO timestamps, nulls instead of blanks),
    each with the consumption since the previous reading and quality flags.

    Readings are cached per meter (see `/v1/status`). If the portal can't be reached, a
    cached copy is served with `freshness.stale = true`: within about 3 s the first time,
    then straight away for the next minute.
    """
    params = parse_window(start, end)  # a bad window is a 422 before the portal is asked anything
    meter = await asyncio.to_thread(_meter_row, services, meter_id)
    rows, freshness = await _load_series(services, meter["meter_id"])
    latest = _last_reading_at(rows)
    window = resolve_window(params, latest)
    selected = [r for r in rows if r.timestamp in window]
    readings, summary = build_readings(selected, detect_interval_minutes([r.timestamp for r in rows]), history=rows)
    return ReadingsResponse(
        meter_id=meter["meter_id"],
        start=window.start,
        end=window.end,
        last_reading_at=latest,
        summary=summary,
        readings=readings,
        freshness=freshness,
    )


@router.get(
    "/{meter_id}/consumption",
    response_model=ConsumptionResponse,
    responses=problems(401, 404, 422, 502, 503),
    summary="Consumption per hour or day",
)
async def get_consumption(
    meter_id: MeterId,
    services: IndexedServices,
    start: FromParam = None,
    end: ToParam = None,
    granularity: Annotated[
        Granularity, Query(description="Calendar buckets (IST). `hour` needs a half-hourly meter.")
    ] = "day",
) -> ConsumptionResponse:
    """Energy used per calendar day or hour (IST), from register values at bucket
    boundaries. Every bucket the window touches is returned, as far as the cached data
    reaches (an end exactly on a boundary closes the bucket before it); buckets that
    can't be fully computed are marked `complete: false`.
    """
    params = parse_window(start, end)
    meter = await asyncio.to_thread(_meter_row, services, meter_id)
    meter_id = meter["meter_id"]
    rows, freshness = await _load_series(services, meter_id)
    interval = detect_interval_minutes([r.timestamp for r in rows])
    if granularity == "hour" and interval and interval > 60:
        raise ApiProblem(
            422,
            "granularity_unavailable",
            f"Meter {meter_id} reports every {interval} minutes; hourly consumption needs sub-hourly readings.",
        )
    valid = [r for r in rows if r.register_kwh is not None]
    latest = valid[-1].timestamp if valid else None
    window = resolve_window(params, latest)
    buckets = []
    if window.start is not None and window.end is not None and valid:
        # Only buckets the data can speak to: everything outside the cached series would be
        # null anyway, and an unclamped window could ask for millions of empty buckets.
        lo, hi = max(window.start, valid[0].timestamp), min(window.end, valid[-1].timestamp)
        if lo <= hi:
            buckets = consumption_buckets(rows, granularity, bucket_floor(lo, granularity), hi)
    total = None
    if buckets:
        span = register_span(rows, "register_kwh", buckets[0].start, buckets[-1].end)
        total = None if span.decreased else span.total
    return ConsumptionResponse(
        meter_id=meter_id,
        granularity=granularity,
        start=buckets[0].start if buckets else None,
        end=buckets[-1].end if buckets else None,
        last_reading_at=latest,
        total_kwh=total,
        buckets=buckets,
        freshness=freshness,
    )


@router.get(
    "/{meter_id}/anomalies",
    response_model=MeterAnomalies,
    responses=problems(401, 404, 422, 502, 503),
    summary="Anomalies detected in one meter's readings",
)
async def get_meter_anomalies(meter_id: MeterId, services: IndexedServices) -> MeterAnomalies:
    """Every anomaly rule (listed under `/v1/insights/anomalies`) run over the meter's
    whole cached series, most severe first."""
    meter = await asyncio.to_thread(_meter_row, services, meter_id)
    rows, _ = await _load_series(services, meter["meter_id"])
    return MeterAnomalies(
        meter_id=meter["meter_id"],
        status=MeterStatus(meter["status"]),
        transformer_code=meter["transformer_code"],
        anomalies=anomalies_out(detect_for_meter(meter, rows, now=services.now())),
    )


def _meter_row(services: Services, raw_id: str) -> sqlite3.Row:
    meter_id = normalize_meter_id(raw_id)
    row = services.store.get_meter(meter_id)
    if row is None:
        raise ApiProblem(404, "meter_not_found", f"No meter with id {meter_id!r}.")
    return row


async def _load_series(services: Services, meter_id: str) -> tuple[list[StoredReading], Freshness]:
    fetched_at, stale = await services.sync.ensure_readings(meter_id)
    rows = await asyncio.to_thread(services.store.get_readings, meter_id)
    return [StoredReading.from_row(r) for r in rows], Freshness(fetched_at=fetched_at, stale=stale)


def _last_reading_at(rows: list[StoredReading]) -> datetime | None:
    # The latest reading that actually carries a register value, not just the last row.
    valid = [r.timestamp for r in rows if r.register_kwh is not None]
    return max(valid) if valid else None
