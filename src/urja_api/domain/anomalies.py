"""Per-meter anomaly detection over a register series.

The portal's data is synthetic and perfectly regular (straight-line registers, voltage
uniform in 220–240 V), so none of these thresholds could be tuned on it. They come from
physics and standards instead, and each rule is proven against injected faults in the
test suite:

* voltage bands: 230 V nominal. ±6 % (216.2–243.8 V) is India's statutory LV supply
  tolerance, ±10 % (207–253 V) is clearly abnormal, and below 50 V means no supply;
* implausible load: the most a meter of that class can physically pass, i.e. 60 A
  single-phase whole-current, 100 A three-phase whole-current, ~400 A CT-operated (per phase);
* registers have 0.01 resolution, so comparisons allow two steps of slack, applied to
  the *cumulative* change: a slow but steady drift is caught even when every single step
  is within the slack.

On the current data the rules that fire are `decommissioned_reporting` (every one of the
75 decommissioned meters is still consuming), `duplicate_timestamps` (5 meters) and
`stale` (every meter: the data stopped in June 2026).
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise

from .models import AnomalyRule, InstallationType, MeterStatus, Phase, Severity
from .readings import (
    CONFLICTING_DUPLICATE,
    DUPLICATE,
    REGISTER_TOLERANCE,
    StoredReading,
    consumption_buckets,
    register_span,
    rises,
)

NOMINAL_V = 230.0
STATUTORY_BAND = (216.2, 243.8)  # ±6 %
ABNORMAL_BAND = (207.0, 253.0)  # ±10 %
NO_SUPPLY_V = 50.0
FLATLINE = timedelta(hours=24)
SURGE_FACTOR, SURGE_MIN_KWH = 5.0, 5.0

RULE_SEVERITY: dict[AnomalyRule, Severity] = {
    "duplicate_timestamps": "info",
    "conflicting_duplicates": "warning",
    "missing_values": "warning",
    "gaps": "warning",
    "register_decrease": "error",
    "implausible_load": "error",
    "flatline": "warning",
    "consumption_surge": "warning",
    "power_factor_above_one": "error",
    "no_voltage": "error",
    "voltage_outside_10pct": "error",
    "voltage_outside_6pct": "warning",
    "decommissioned_reporting": "error",
    "stale": "warning",
}
SEVERITY_RANK: dict[Severity, int] = {"info": 0, "warning": 1, "error": 2}


@dataclass(frozen=True)
class Anomaly:
    rule: AnomalyRule
    severity: Severity
    message: str
    occurrences: int = 1
    first_at: datetime | None = None
    last_at: datetime | None = None


def ranked(anomalies: Iterable[Anomaly]) -> list[Anomaly]:
    """Most severe first, then by rule name."""
    return sorted(anomalies, key=lambda a: (-SEVERITY_RANK[a.severity], a.rule))


def max_load_kw(phase: Phase, installation: InstallationType) -> float:
    phases = 3 if phase is Phase.three else 1
    amps = 400 if installation is InstallationType.ct_operated else (100 if phases == 3 else 60)
    return phases * NOMINAL_V * amps / 1000  # 13.8 / 69 kW whole-current, 92 / 276 kW CT-operated


def detect(
    rows: Sequence[StoredReading],
    *,
    status: MeterStatus,
    phase: Phase,
    installation: InstallationType,
    interval_minutes: int | None,
    now: datetime | None,
) -> list[Anomaly]:
    """Run every rule over one meter's series. With `now=None` the clock-dependent
    `stale` rule is skipped, so the result can be cached and `stale` checked per request."""
    found: list[Anomaly] = []

    def add(rule: AnomalyRule, message: str, times: Sequence[datetime]) -> None:
        if times:
            found.append(Anomaly(rule, RULE_SEVERITY[rule], message, len(times), min(times), max(times)))

    # --- data integrity -------------------------------------------------------------------
    add(
        "duplicate_timestamps",
        "The portal sent the same timestamp more than once (merged into one reading).",
        [r.timestamp for r in rows if r.duplicate == DUPLICATE],
    )
    add(
        "conflicting_duplicates",
        "The portal sent different values for the same timestamp.",
        [r.timestamp for r in rows if r.duplicate == CONFLICTING_DUPLICATE],
    )
    add(
        "missing_values",
        "Readings with a blank kWh register or voltage.",
        [r.timestamp for r in rows if r.register_kwh is None or r.voltage_v is None],
    )
    if interval_minutes:
        interval = timedelta(minutes=interval_minutes)
        add(
            "gaps",
            f"Missing readings (expected one every {interval_minutes} min).",
            [b.timestamp for a, b in pairwise(rows) if b.timestamp - a.timestamp > interval],
        )

    # --- register behaviour ----------------------------------------------------------------
    valid = [r for r in rows if r.register_kwh is not None]
    # Each rise is measured against the highest value so far (None: the register went
    # down), so a slow reverse run is caught and read noise creates no energy.
    steps = list(rises(valid, "register_kwh"))
    add(
        "register_decrease",
        "The kWh register went down (reset, rollover, meter replacement or a bad read).",
        [b.timestamp for _, b, rise in steps if rise is None],
    )

    limit_kw = max_load_kw(phase, installation)
    add(
        "implausible_load",
        f"Average load above what this meter class can carry ({limit_kw:.1f} kW).",
        [
            b.timestamp
            for a, b, rise in steps
            if rise and (hours := (b.timestamp - a.timestamp).total_seconds() / 3600) > 0 and rise / hours > limit_kw
        ],
    )

    if status is not MeterStatus.decommissioned:
        flat_since: datetime | None = None
        flat_ends: list[datetime] = []
        for a, b, rise in steps:
            if rise == 0:
                flat_since = flat_since or a.timestamp
                if b.timestamp - flat_since >= FLATLINE and (not flat_ends or flat_ends[-1] < flat_since):
                    flat_ends.append(b.timestamp)
            else:
                flat_since = None
        add("flatline", "No consumption at all for 24 hours or more.", flat_ends)

    if valid:
        days = consumption_buckets(valid, "day", valid[0].timestamp, valid[-1].timestamp)
        complete = [d for d in days if d.complete and d.consumption_kwh is not None]
        if len(complete) >= 3:
            median = statistics.median(d.consumption_kwh for d in complete)  # type: ignore[misc]
            add(
                "consumption_surge",
                f"Days using more than {SURGE_FACTOR:g}x the meter's median ({median:.2f} kWh).",
                [
                    d.start
                    for d in complete
                    if d.consumption_kwh > max(SURGE_FACTOR * median, SURGE_MIN_KWH)  # type: ignore[operator]
                ],
            )
        add(
            "power_factor_above_one",
            "On a complete day the kVAh register rose less than the kWh register (physically impossible).",
            [
                d.start
                for d in complete
                if d.apparent_kvah is not None and d.apparent_kvah < d.consumption_kwh - REGISTER_TOLERANCE  # type: ignore[operator]
            ],
        )

    # --- supply voltage ------------------------------------------------------------------
    volts = [(r.timestamp, r.voltage_v) for r in rows if r.voltage_v is not None]
    add("no_voltage", f"Voltage below {NO_SUPPLY_V:g} V (no supply).", [t for t, v in volts if v < NO_SUPPLY_V])
    add(
        "voltage_outside_10pct",
        f"Voltage outside {ABNORMAL_BAND[0]:g}–{ABNORMAL_BAND[1]:g} V (±10 % of 230 V).",
        [t for t, v in volts if v >= NO_SUPPLY_V and not ABNORMAL_BAND[0] <= v <= ABNORMAL_BAND[1]],
    )
    add(
        "voltage_outside_6pct",
        f"Voltage outside the statutory {STATUTORY_BAND[0]:g}–{STATUTORY_BAND[1]:g} V band (±6 %).",
        [
            t
            for t, v in volts
            if ABNORMAL_BAND[0] <= v <= ABNORMAL_BAND[1] and not STATUTORY_BAND[0] <= v <= STATUTORY_BAND[1]
        ],
    )

    # --- status vs. behaviour --------------------------------------------------------------
    rise = register_span(valid, "register_kwh").total
    if status is MeterStatus.decommissioned and rise is not None and rise > REGISTER_TOLERANCE:
        add(
            "decommissioned_reporting",
            f"Meter is marked decommissioned but its register rose {rise:,.2f} kWh "
            "(possible unbilled consumption or a stale status).",
            [b.timestamp for _, b, rise in steps if rise],
        )

    if valid and now is not None and (stale := stale_anomaly(valid[-1].timestamp, interval_minutes, now)):
        found.append(stale)
    return found


def stale_anomaly(last_reading_at: datetime, interval_minutes: int | None, now: datetime) -> Anomaly | None:
    """`stale`: no reading for more than two intervals (at least two days) before `now`."""
    allowed = max(timedelta(minutes=interval_minutes or 0), timedelta(hours=24)) * 2
    if now - last_reading_at <= allowed:
        return None
    days_old = (now - last_reading_at).total_seconds() / 86400
    return Anomaly(
        "stale",
        RULE_SEVERITY["stale"],
        f"Latest reading is {days_old:.0f} days old.",
        1,
        last_reading_at,
        last_reading_at,
    )
