"""Fleet-wide questions the portal itself cannot answer.

Both endpoints work from the local cache only (reference index plus cached readings), so
they cost the portal nothing. The background warm-up fills the readings cache, and each
response states how many meters it covered, so a partial cache is never mistaken for the
fleet. The aggregation itself lives in `derived.py`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Query

from ...derived import consumption_by_group, summarise_anomalies
from ...domain.models import (
    AnomalyReport,
    AnomalyRule,
    ConsumptionGroup,
    ConsumptionInsight,
    GroupBy,
    MeterAnomalies,
    MeterStatus,
    NetworkLevel,
    Severity,
)
from ...domain.readings import bucket_floor
from ...store import LEVEL_COLUMNS
from ..deps import IndexedServices, Services
from ..errors import problems
from ..views import anomalies_out, parse_window, resolve_window

router = APIRouter(tags=["insights"])

GROUP_COLUMNS = {
    **LEVEL_COLUMNS,
    "status": "status",
    "make": "make",
    "phase": "phase",
    "installation_type": "installation_type",
    "meter": "meter_id",
}


@router.get(
    "/v1/insights/anomalies",
    response_model=AnomalyReport,
    responses=problems(401, 422, 503),
    summary="Fleet-wide anomaly report",
)
def anomaly_report(
    services: IndexedServices,
    rule: Annotated[AnomalyRule | None, Query(description="Only list meters triggering this rule.")] = None,
    severity: Annotated[Severity | None, Query(description="Minimum severity to count and list.")] = None,
    include_meters: Annotated[bool, Query(description="Set to false for just the counts.")] = True,
) -> AnomalyReport:
    """Runs every anomaly rule over every cached series. Rules and severities:
    data integrity (`duplicate_timestamps` info, `conflicting_duplicates`, `missing_values`,
    `gaps` warning), register behaviour (`register_decrease`, `implausible_load` error;
    `flatline`, `consumption_surge` warning; `power_factor_above_one` error), supply voltage
    (`no_voltage`, `voltage_outside_10pct` error; `voltage_outside_6pct` warning), status
    (`decommissioned_reporting` error) and freshness (`stale` warning: no reading for two
    intervals, at least two days).
    """
    rules = services.derived.rules()
    summary = summarise_anomalies(rules, services.now(), rule=rule, min_severity=severity)
    meters = services.store.all_meters()
    listed = [
        MeterAnomalies(
            meter_id=meter_id,
            status=MeterStatus(meters[meter_id]["status"]),
            transformer_code=meters[meter_id]["transformer_code"],
            anomalies=anomalies_out(found),
        )
        for meter_id, found in summary.meters
        if meter_id in meters  # a sync may have removed it since the rules were computed
    ]
    return AnomalyReport(
        meters_analysed=len(rules),
        meters_total=len(meters),
        meters_by_rule=summary.meters_by_rule,
        rule_severity=summary.rule_severity,
        meters_by_severity=summary.meters_by_severity,
        meters=listed if include_meters else [],
    )


@router.get(
    "/v1/insights/consumption",
    response_model=ConsumptionInsight,
    responses=problems(401, 422, 503),
    summary="Consumption grouped by any dimension",
)
def consumption_insight(
    services: IndexedServices,
    group_by: Annotated[GroupBy, Query(description="A network level, a meter attribute, or `meter`.")] = "transformer",
    start: Annotated[
        str | None,
        Query(alias="from", description="First day, `YYYY-MM-DD` (IST). Defaults to 7 days before `to`."),
    ] = None,
    end: Annotated[
        str | None,
        Query(
            alias="to",
            description="Last day, `YYYY-MM-DD`, inclusive (a datetime exactly at midnight closes the day "
            "before). Defaults to the latest cached reading.",
        ),
    ] = None,
    include_decommissioned: Annotated[
        bool, Query(description="Decommissioned meters still report consumption; include them?")
    ] = True,
    limit: Annotated[
        int, Query(ge=1, le=500, description="Return the top `limit` groups by consumption; see `groups_total`.")
    ] = 50,
) -> ConsumptionInsight:
    """Total energy per network node or meter attribute, over **complete IST days** only
    (a day needs a reading at both midnights). The window is widened to whole days. For
    example: `group_by=transformer` to see transformer loading, `group_by=meter` for top
    consumers, or `group_by=status` to see how much energy decommissioned meters use.
    """
    params = parse_window(start, end)
    series = services.derived.series()
    latest = max((rows[-1].timestamp for rows in series.values() if rows), default=None)
    window = resolve_window(params, latest)
    first_day = last_day = None
    if window.start is not None and window.end is not None:
        first_day = bucket_floor(window.start, "day")
        # Exclusive, and like /consumption: an end exactly at midnight closes the day before.
        last_day = max(first_day, bucket_floor(window.end - timedelta(microseconds=1), "day") + timedelta(days=1))
    meters = services.store.all_meters()
    totals = (
        consumption_by_group(
            meters,
            services.derived.daily(),
            GROUP_COLUMNS[group_by],
            first_day,
            last_day,
            include_decommissioned=include_decommissioned,
        )
        if first_day is not None and last_day is not None
        else None
    )
    groups = totals.groups if totals else []
    grand = totals.total_kwh if totals else None
    names = _group_names(services, group_by)
    return ConsumptionInsight(
        group_by=group_by,
        start=first_day,
        end=last_day,
        days_counted=len(totals.days) if totals else 0,
        first_day=totals.days[0].date() if totals and totals.days else None,
        last_day=totals.days[-1].date() if totals and totals.days else None,
        include_decommissioned=include_decommissioned,
        meters_analysed=totals.meters_analysed if totals else 0,
        meters_total=len(meters),
        total_kwh=grand,
        groups_total=len(groups),
        groups=[
            ConsumptionGroup(
                key=g.key,
                name=names.get(g.key),
                meter_count=g.meters,
                meters_with_data_count=g.meters_with_data,
                consumption_kwh=g.consumption_kwh,
                share=round(g.consumption_kwh / grand, 4) if grand else 0.0,
            )
            for g in groups[:limit]
        ],
        note="Only complete IST days are counted (a day needs a reading at both midnights). "
        "Meters without cached readings are counted in `meter_count` but not in `meters_with_data_count`.",
    )


def _group_names(services: Services, group_by: GroupBy) -> dict[str, str]:
    if group_by == "transformer":
        return {r["code"]: r["name"] for r in services.store.list_transformers()}
    names: dict[str, str] = {}
    if group_by in NetworkLevel.__members__:
        for path in services.store.transformer_paths().values():
            ref = getattr(path, group_by)
            if ref.name:
                names[ref.code] = ref.name
    return names
