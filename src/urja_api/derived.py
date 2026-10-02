"""Fleet-wide views derived from the local cache, memoised per cache version.

The insight endpoints need every meter's readings at once (~70k rows). Parsing them and
running the consumption and anomaly logic costs about a second of CPU, but the underlying
data only changes when the portal's does: unchanged readings payloads and unchanged
reference data are recognised by their hash and don't bump the store's versions. Each
view is therefore computed once per (readings, reference data) version and reused until
something actually changes.

The aggregations over those views (`summarise_anomalies`, `consumption_by_group`) are
plain functions, so the routes only parse parameters and shape responses.
"""

from __future__ import annotations

import threading
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .domain.anomalies import SEVERITY_RANK, Anomaly, detect, ranked, stale_anomaly
from .domain.models import AnomalyRule, ConsumptionBucket, InstallationType, MeterStatus, Phase, Severity
from .domain.readings import StoredReading, consumption_buckets, detect_interval_minutes
from .store import Store

# A meter record as the store returns it (anything with these keys works, e.g. a dict).
MeterRow = Mapping[str, Any]


def detect_for_meter(meter: MeterRow, rows: Sequence[StoredReading], now: datetime | None) -> list[Anomaly]:
    """Every anomaly rule for one meter; `now=None` leaves out the clock-dependent `stale`."""
    return detect(
        rows,
        status=MeterStatus(meter["status"]),
        phase=Phase(meter["phase"]),
        installation=InstallationType(meter["installation_type"]),
        interval_minutes=detect_interval_minutes([r.timestamp for r in rows]),
        now=now,
    )


@dataclass(frozen=True)
class MeterRules:
    """A meter's anomaly results, minus the clock-dependent `stale` rule."""

    anomalies: list[Anomaly]
    last_reading_at: datetime | None
    interval_minutes: int | None


class DerivedViews:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._series: tuple[int, dict[str, list[StoredReading]]] | None = None
        self._daily: tuple[int, dict[str, list[ConsumptionBucket]]] | None = None
        self._rules: tuple[tuple[int, int], dict[str, MeterRules]] | None = None
        # The insight routes run in worker threads: one of them recomputes a stale view,
        # the others wait for it instead of each repeating the work.
        self._lock = threading.RLock()

    def series(self) -> dict[str, list[StoredReading]]:
        """Every cached series, parsed."""
        with self._lock:
            version = self.store.readings_version  # read first, so a concurrent write re-triggers
            if self._series is None or self._series[0] != version:
                parsed = {
                    meter_id: [StoredReading.from_row(r) for r in rows]
                    for meter_id, rows in self.store.readings_for_all().items()
                }
                self._series = (version, parsed)
            return self._series[1]

    def daily(self) -> dict[str, list[ConsumptionBucket]]:
        """Per-meter daily consumption over each meter's whole cached series."""
        with self._lock:
            version = self.store.readings_version
            if self._daily is None or self._daily[0] != version:
                buckets = {
                    meter_id: consumption_buckets(rows, "day", rows[0].timestamp, rows[-1].timestamp)
                    for meter_id, rows in self.series().items()
                    if rows
                }
                self._daily = (version, buckets)
            return self._daily[1]

    def rules(self) -> dict[str, MeterRules]:
        """Anomaly results per cached meter. They depend on meter status too, so they are
        keyed on the reference-data version as well. `stale` is evaluated per request."""
        with self._lock:
            return self._locked_rules()

    def _locked_rules(self) -> dict[str, MeterRules]:
        key = (self.store.readings_version, self.store.reference_version)
        if self._rules is None or self._rules[0] != key:
            meters = self.store.all_meters()
            results: dict[str, MeterRules] = {}
            for meter_id, rows in self.series().items():
                meter = meters.get(meter_id)
                if meter is None:
                    continue
                valid = [r.timestamp for r in rows if r.register_kwh is not None]
                results[meter_id] = MeterRules(
                    detect_for_meter(meter, rows, now=None),
                    max(valid) if valid else None,
                    detect_interval_minutes([r.timestamp for r in rows]),
                )
            self._rules = (key, results)
        return self._rules[1]


# ----------------------------------------------------------------------------- aggregations


@dataclass(frozen=True)
class AnomalySummary:
    meters_by_rule: dict[AnomalyRule, int]
    rule_severity: dict[AnomalyRule, Severity]
    meters_by_severity: dict[Severity, int]
    meters: list[tuple[str, list[Anomaly]]]  # (meter id, its anomalies, most severe first)


def summarise_anomalies(
    rules: Mapping[str, MeterRules],
    now: datetime,
    *,
    rule: AnomalyRule | None = None,
    min_severity: Severity | None = None,
) -> AnomalySummary:
    """Count meters per rule and severity, and list the meters worth looking at.

    With `rule`, the list holds the meters triggering it, each with all of its anomalies
    for context. Without, it leaves out meters whose only anomaly is `stale`: that rule
    depends on the clock, and once the portal's data stops, it matches every meter.
    Either way the counts cover every rule.
    """
    floor = SEVERITY_RANK[min_severity] if min_severity else 0
    by_rule: Counter[AnomalyRule] = Counter()
    by_severity: Counter[Severity] = Counter()
    severity_of: dict[AnomalyRule, Severity] = {}
    listed: list[tuple[str, list[Anomaly]]] = []
    for meter_id, result in sorted(rules.items()):
        stale = result.last_reading_at and stale_anomaly(result.last_reading_at, result.interval_minutes, now)
        found = [a for a in (*result.anomalies, *([stale] if stale else [])) if SEVERITY_RANK[a.severity] >= floor]
        by_rule.update({a.rule for a in found})
        by_severity.update({a.severity for a in found})
        severity_of.update((a.rule, a.severity) for a in found)
        wanted = any(a.rule == rule for a in found) if rule else any(a.rule != "stale" for a in found)
        if wanted:
            listed.append((meter_id, ranked(found)))
    return AnomalySummary(
        meters_by_rule=dict(sorted(by_rule.items())),
        rule_severity=dict(sorted(severity_of.items())),
        meters_by_severity={s: by_severity[s] for s in ("error", "warning", "info") if by_severity[s]},
        meters=listed,
    )


@dataclass(frozen=True)
class GroupTotal:
    key: str
    meters: int
    meters_with_data: int
    consumption_kwh: float


@dataclass(frozen=True)
class ConsumptionTotals:
    groups: list[GroupTotal]  # biggest consumer first
    meters_analysed: int
    total_kwh: float | None  # None when no meter with cached readings was included
    days: list[datetime]  # the distinct complete days counted, in order


def consumption_by_group(
    meters: Mapping[str, MeterRow],
    daily: Mapping[str, Sequence[ConsumptionBucket]],
    column: str,
    start: datetime,
    end: datetime,
    *,
    include_decommissioned: bool = True,
) -> ConsumptionTotals:
    """Energy per value of `column` over the complete IST days that start in [start, end).

    Every meter counts towards its group's `meters`; only meters with cached readings
    count as analysed, and only complete days (a reading at both midnights) are summed.
    """
    totals: defaultdict[str, float] = defaultdict(float)
    members: Counter[str] = Counter()
    with_data: Counter[str] = Counter()
    days: set[datetime] = set()
    analysed = 0
    for meter_id, meter in meters.items():
        if not include_decommissioned and meter["status"] == MeterStatus.decommissioned:
            continue
        key = meter[column]
        members[key] += 1
        buckets = daily.get(meter_id)
        if not buckets:
            continue
        analysed += 1
        complete = [b for b in buckets if b.complete and b.consumption_kwh is not None and start <= b.start < end]
        if complete:
            days.update(b.start for b in complete)
            totals[key] += sum(b.consumption_kwh for b in complete)  # type: ignore[misc]
            with_data[key] += 1
    groups = sorted(
        (GroupTotal(key, members[key], with_data[key], round(totals[key], 2)) for key in members),
        key=lambda g: (-g.consumption_kwh, g.key),
    )
    total = round(sum(totals.values()), 2) if analysed else None
    return ConsumptionTotals(groups, analysed, total, sorted(days))
