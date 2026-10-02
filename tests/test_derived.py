"""The fleet aggregations behind /v1/insights, as plain functions over cached results."""

from __future__ import annotations

from datetime import datetime, timedelta

from urja_api.derived import MeterRules, consumption_by_group, summarise_anomalies
from urja_api.domain.anomalies import RULE_SEVERITY, Anomaly
from urja_api.domain.models import ConsumptionBucket
from urja_api.domain.normalize import IST

JUNE_1 = datetime(2026, 6, 1, tzinfo=IST)


def anomaly(rule: str) -> Anomaly:
    return Anomaly(rule, RULE_SEVERITY[rule], rule)  # type: ignore[arg-type]


def rules(**by_meter: list[str]) -> dict[str, MeterRules]:
    """Every meter's last reading is on 1 June at midnight, read once a day."""
    return {m: MeterRules([anomaly(r) for r in found], JUNE_1, 1440) for m, found in by_meter.items()}


def test_counts_cover_every_rule_but_merely_stale_meters_are_not_listed():
    summary = summarise_anomalies(
        rules(A=["decommissioned_reporting"], B=[], C=["duplicate_timestamps", "gaps"]),
        JUNE_1 + timedelta(days=10),  # every meter is stale by now
    )
    assert summary.meters_by_rule == {"decommissioned_reporting": 1, "duplicate_timestamps": 1, "gaps": 1, "stale": 3}
    assert summary.meters_by_severity == {"error": 1, "warning": 3, "info": 1}
    assert summary.rule_severity["stale"] == "warning"
    listed = {meter: [a.rule for a in found] for meter, found in summary.meters}
    # B is only stale; the others are listed with every anomaly, most severe first.
    assert listed == {"A": ["decommissioned_reporting", "stale"], "C": ["gaps", "stale", "duplicate_timestamps"]}


def test_a_rule_filter_lists_its_meters_and_a_severity_floor_drops_the_rest():
    found = rules(A=["decommissioned_reporting"], B=[], C=["duplicate_timestamps"])
    later = JUNE_1 + timedelta(days=10)
    assert [m for m, _ in summarise_anomalies(found, later, rule="stale").meters] == ["A", "B", "C"]
    errors = summarise_anomalies(found, later, min_severity="error")
    assert errors.meters_by_rule == {"decommissioned_reporting": 1}
    assert [m for m, _ in errors.meters] == ["A"]
    assert summarise_anomalies(found, JUNE_1 + timedelta(hours=12)).meters_by_rule == {
        "decommissioned_reporting": 1,
        "duplicate_timestamps": 1,
    }  # not stale yet


def day(n: int, kwh: float | None, *, complete: bool = True) -> ConsumptionBucket:
    start = JUNE_1 + timedelta(days=n - 1)
    return ConsumptionBucket(
        start=start,
        end=start + timedelta(days=1),
        consumption_kwh=kwh,
        apparent_kvah=None,
        power_factor=None,
        coverage=1.0 if complete else 0.5,
        complete=complete,
    )


METERS = {
    "A": {"status": "installed", "transformer_code": "DT-1"},
    "B": {"status": "decommissioned", "transformer_code": "DT-1"},
    "C": {"status": "installed", "transformer_code": "DT-2"},
    "D": {"status": "installed", "transformer_code": "DT-3"},  # nothing cached
}
DAILY = {
    "A": [day(1, 10.0), day(2, 12.0), day(3, 5.0, complete=False)],
    "B": [day(1, 1.0), day(2, 2.0)],
    "C": [day(2, 30.0), day(3, 40.0)],
}


def test_complete_days_inside_the_window_are_summed_per_group():
    totals = consumption_by_group(METERS, DAILY, "transformer_code", JUNE_1, JUNE_1 + timedelta(days=2))
    assert [(g.key, g.meters, g.meters_with_data, g.consumption_kwh) for g in totals.groups] == [
        ("DT-2", 1, 1, 30.0),  # biggest consumer first
        ("DT-1", 2, 2, 25.0),  # days 1-2 of A and B
        ("DT-3", 1, 0, 0.0),  # D has nothing cached: counted as a member, not as data
    ]
    assert (totals.meters_analysed, totals.total_kwh) == (3, 55.0)
    assert totals.days == [JUNE_1, JUNE_1 + timedelta(days=1)]


def test_an_incomplete_day_is_never_counted():
    totals = consumption_by_group(METERS, DAILY, "status", JUNE_1 + timedelta(days=2), JUNE_1 + timedelta(days=3))
    assert {g.key: g.consumption_kwh for g in totals.groups} == {"installed": 40.0, "decommissioned": 0.0}
    assert totals.days == [JUNE_1 + timedelta(days=2)]


def test_decommissioned_meters_can_be_left_out():
    totals = consumption_by_group(
        METERS, DAILY, "status", JUNE_1, JUNE_1 + timedelta(days=3), include_decommissioned=False
    )
    assert [g.key for g in totals.groups] == ["installed"]
    assert (totals.meters_analysed, totals.total_kwh) == (2, 92.0)


def test_nothing_cached_is_no_total_rather_than_zero():
    totals = consumption_by_group(METERS, {}, "status", JUNE_1, JUNE_1 + timedelta(days=3))
    assert (totals.meters_analysed, totals.total_kwh, totals.days) == (0, None, [])
    assert {g.key: g.meters for g in totals.groups} == {"installed": 3, "decommissioned": 1}
