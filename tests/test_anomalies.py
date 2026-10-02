"""Anomaly rules, each proven by injecting exactly one fault into an otherwise clean series.

The portal's own data is perfectly regular, so the rules cannot be checked against real
faults. Instead every test starts from a synthetic series shaped like the portal's (straight
registers, kVAh = 1.08 x kWh, steady voltage) that triggers nothing, injects one fault and
asserts that exactly the matching rule fires.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from urja_api.domain.anomalies import Anomaly, detect, max_load_kw
from urja_api.domain.models import InstallationType, MeterStatus, Phase
from urja_api.domain.normalize import IST, RawReading
from urja_api.domain.readings import StoredReading, detect_interval_minutes, merge_duplicates

START = datetime(2026, 6, 1, tzinfo=IST)
HALF_HOURLY, DAILY = 30, 1440
VARIANTS = pytest.mark.parametrize("interval", [HALF_HOURLY, DAILY], ids=["half-hourly", "daily"])
Row = dict[str, Any]


def clean(interval: int, *, kw: float = 1.0, days: int = 6) -> list[Row]:
    """A constant `kw` load, PF 1/1.08, 230 V: six complete days (with the closing midnight)."""
    step_kwh = kw * interval / 60
    return [
        {
            "at": START + timedelta(minutes=i * interval),
            "kwh": 1_000 + i * step_kwh,
            "kvah": 1_080 + i * step_kwh * 1.08,
            "volts": 230.0,
        }
        for i in range(days * 1440 // interval + 1)
    ]


def index_at(rows: list[Row], day: int, hour: int = 0) -> int:
    ts = START + timedelta(days=day - 1, hours=hour)
    return next(i for i, row in enumerate(rows) if row["at"] == ts)


def midday(rows: list[Row], interval: int) -> int:
    """A reading inside day 3 (for a daily series, the day's only reading)."""
    return index_at(rows, 3, 12 if interval == HALF_HOURLY else 0)


def per_day(interval: int) -> int:
    return 1440 // interval


def add_energy(rows: list[Row], start: int, steps: int, kwh: float, kvah: float | None = None) -> None:
    """Spread extra energy evenly over the `steps` intervals after rows[start]; negative = less."""
    kvah = kwh * 1.08 if kvah is None else kvah
    for offset, row in enumerate(rows[start + 1 :], start=1):
        share = min(offset, steps) / steps
        row["kwh"] += kwh * share
        row["kvah"] += kvah * share


def analyse(
    rows: list[Row],
    *,
    status: MeterStatus = MeterStatus.installed,
    phase: Phase = Phase.single,
    installation: InstallationType = InstallationType.whole_current,
    now: datetime | None = None,
) -> dict[str, Anomaly]:
    """Run the rules the way the API does: 0.01-resolution registers, duplicates merged."""

    def register(value: float | None) -> float | None:
        return None if value is None else round(value, 2)

    raw = [RawReading(r["at"], register(r["kwh"]), register(r["kvah"]), r["volts"]) for r in rows]
    merged, duplicates = merge_duplicates(raw)
    series = [
        StoredReading(m.timestamp, m.energy_kwh, m.energy_kvah, m.voltage_v, duplicates.get(m.timestamp, 0))
        for m in merged
    ]
    found = detect(
        series,
        status=status,
        phase=phase,
        installation=installation,
        interval_minutes=detect_interval_minutes([r.timestamp for r in series]),
        now=now or series[-1].timestamp + timedelta(hours=1),
    )
    return {a.rule: a for a in found}


# ----------------------------------------------------------------------------- baseline


@VARIANTS
@pytest.mark.parametrize("status", [MeterStatus.installed, MeterStatus.faulty, MeterStatus.unknown])
def test_a_clean_series_triggers_nothing(interval, status):
    assert analyse(clean(interval), status=status) == {}


def test_real_meters_trigger_only_what_their_data_warrants(meters, stored):
    found = {}
    for meter in meters:
        rows = stored(meter.meter_id)
        anomalies = detect(
            rows,
            status=meter.status,
            phase=meter.phase,
            installation=meter.installation_type,
            interval_minutes=detect_interval_minutes([r.timestamp for r in rows]),
            now=rows[-1].timestamp + timedelta(hours=1),
        )
        if anomalies:
            found[meter.meter_id] = {a.rule for a in anomalies}
    decommissioned = {"decommissioned_reporting"}
    assert found == {
        "J100000": decommissioned,
        "J100006": decommissioned,
        "J100011": decommissioned,
        "J100100": decommissioned,
        "J100089": {"duplicate_timestamps"},  # the blank 30/06 row
    }


# ----------------------------------------------------------------------------- registers


@VARIANTS
def test_register_decrease(interval):
    rows = clean(interval)
    k = midday(rows, interval)
    add_energy(rows, k - 1, 1, kwh=-500)  # e.g. a meter swap
    found = analyse(rows)
    assert set(found) == {"register_decrease"}
    assert (found["register_decrease"].occurrences, found["register_decrease"].first_at) == (1, rows[k]["at"])


@VARIANTS
def test_a_dip_within_register_resolution_is_not_a_decrease(interval):
    rows = clean(interval)
    k = midday(rows, interval)
    for register in ("kwh", "kvah"):  # one read 0.01 below the previous one
        rows[k][register] = rows[k - 1][register] - 0.01
    found = analyse(rows)
    assert "register_decrease" not in found
    # The dip is zero consumption for one interval; for a daily meter that is a whole day.
    assert set(found) == ({"flatline"} if interval == DAILY else set())


def test_a_register_running_slowly_backwards_is_a_decrease():
    rows = clean(HALF_HOURLY)
    start = len(rows) - 11
    for offset, row in enumerate(rows[start + 1 :], start=1):  # 0.02 down per step: each within the slack
        row["kwh"] = rows[start]["kwh"] - 0.02 * offset
        row["kvah"] = rows[start]["kvah"] - 0.02 * offset
    assert set(analyse(rows)) == {"register_decrease"}  # 0.2 kWh lost in all: far beyond rounding


@VARIANTS
def test_flatline(interval):
    rows = clean(interval)
    add_energy(rows, index_at(rows, 3), per_day(interval), kwh=-24)  # day 3 uses nothing
    found = analyse(rows)
    assert set(found) == {"flatline"}
    assert found["flatline"].first_at == START + timedelta(days=3)


def test_flatline_needs_a_full_24_hours():
    rows = clean(HALF_HOURLY)
    add_energy(rows, index_at(rows, 3), 47, kwh=-23.5)  # 23.5 h without consumption
    assert analyse(rows) == {}


@VARIANTS
def test_no_flatline_for_decommissioned_meters(interval):
    rows = clean(interval, kw=0)
    assert set(analyse(rows)) == {"flatline"}
    assert analyse(rows, status=MeterStatus.decommissioned) == {}  # not consuming is what it should do


@VARIANTS
def test_consumption_surge(interval):
    rows = clean(interval)
    add_energy(rows, index_at(rows, 3), per_day(interval), kwh=110)  # 134 kWh vs a 24 kWh median
    found = analyse(rows)
    assert set(found) == {"consumption_surge"}
    assert found["consumption_surge"].first_at == START + timedelta(days=2)


@VARIANTS
def test_four_times_the_median_is_not_a_surge(interval):
    rows = clean(interval)
    add_energy(rows, index_at(rows, 3), per_day(interval), kwh=72)
    assert analyse(rows) == {}


@pytest.mark.parametrize(
    ("phase", "installation", "limit_kw"),
    [
        (Phase.single, InstallationType.whole_current, 13.8),  # 60 A
        (Phase.three, InstallationType.whole_current, 69.0),  # 3 x 100 A
        (Phase.single, InstallationType.ct_operated, 92.0),  # 400 A on one phase
        (Phase.three, InstallationType.ct_operated, 276.0),  # 3 x 400 A
    ],
)
@pytest.mark.parametrize(("factor", "fires"), [(1.05, True), (0.95, False)])
def test_implausible_load_uses_the_meter_class_limit(phase, installation, limit_kw, factor, fires):
    assert max_load_kw(phase, installation) == pytest.approx(limit_kw)
    rows = clean(HALF_HOURLY, kw=10)
    add_energy(rows, midday(rows, HALF_HOURLY), 1, kwh=(limit_kw * factor - 10) / 2)  # one half hour at that load
    expected = {"implausible_load"} if fires else set()
    assert set(analyse(rows, phase=phase, installation=installation)) == expected


@VARIANTS
def test_power_factor_above_one(interval):
    rows = clean(interval)
    add_energy(rows, index_at(rows, 3), per_day(interval), kwh=0, kvah=-4.32)  # kVAh +21.6 vs kWh +24
    found = analyse(rows)
    assert set(found) == {"power_factor_above_one"}
    assert found["power_factor_above_one"].first_at == START + timedelta(days=2)


def test_a_blank_kvah_value_is_not_a_power_factor_fault():
    # On a daily meter the blank covers the only kVAh step of two complete days.
    rows = clean(DAILY)
    rows[index_at(rows, 3)]["kvah"] = None
    assert "power_factor_above_one" not in analyse(rows)


@VARIANTS
def test_decommissioned_meter_still_consuming(interval):
    rows = clean(interval)
    found = analyse(rows, status=MeterStatus.decommissioned)
    assert set(found) == {"decommissioned_reporting"}
    assert found["decommissioned_reporting"].occurrences == len(rows) - 1


def test_read_noise_on_a_decommissioned_meter_is_not_consumption():
    rows = clean(HALF_HOURLY, kw=0)
    for row in rows[1::2]:  # every other read is one count low: the register never really moves
        row["kwh"] -= 0.01
        row["kvah"] -= 0.01
    assert analyse(rows, status=MeterStatus.decommissioned) == {}


def test_a_decommissioned_meter_consuming_slowly_is_still_reported():
    rows = clean(HALF_HOURLY, kw=0.03)  # 0.015 kWh a step, within rounding; 4.3 kWh over six days
    found = analyse(rows, status=MeterStatus.decommissioned)
    assert set(found) == {"decommissioned_reporting"}
    assert "4.32 kWh" in found["decommissioned_reporting"].message


# ----------------------------------------------------------------------------- voltage


@VARIANTS
@pytest.mark.parametrize(
    ("volts", "rule"),
    [
        (214.0, "voltage_outside_6pct"),
        (245.0, "voltage_outside_6pct"),
        (207.0, "voltage_outside_6pct"),
        (253.0, "voltage_outside_6pct"),
        (206.0, "voltage_outside_10pct"),
        (260.0, "voltage_outside_10pct"),
        (50.0, "voltage_outside_10pct"),
        (49.0, "no_voltage"),
        (0.0, "no_voltage"),
    ],
)
def test_voltage_bands(interval, volts, rule):
    rows = clean(interval)
    k = midday(rows, interval)
    rows[k]["volts"] = volts
    found = analyse(rows)
    assert set(found) == {rule}
    assert (found[rule].occurrences, found[rule].first_at) == (1, rows[k]["at"])


@pytest.mark.parametrize("volts", [216.2, 220.0, 240.0, 243.8])
def test_the_statutory_band_is_normal(volts):
    rows = clean(HALF_HOURLY)
    rows[midday(rows, HALF_HOURLY)]["volts"] = volts
    assert analyse(rows) == {}


# ----------------------------------------------------------------------------- data integrity


@VARIANTS
def test_stale(interval):
    rows = clean(interval)
    last = rows[-1]["at"]
    assert set(analyse(rows, now=last + timedelta(hours=49))) == {"stale"}
    assert analyse(rows, now=last + timedelta(hours=47)) == {}


@VARIANTS
def test_gaps(interval):
    rows = clean(interval)
    missing = rows.pop(midday(rows, interval))
    found = analyse(rows)
    assert set(found) == {"gaps"}
    assert (found["gaps"].occurrences, found["gaps"].first_at) == (1, missing["at"] + timedelta(minutes=interval))


@VARIANTS
@pytest.mark.parametrize("field", ["kwh", "volts"])
def test_missing_values(interval, field):
    rows = clean(interval)
    rows[midday(rows, interval)][field] = None
    assert set(analyse(rows)) == {"missing_values"}


@VARIANTS
def test_duplicate_timestamps(interval):
    rows = clean(interval)
    rows.append({**rows[midday(rows, interval)], "kwh": None, "volts": None})  # the portal's blank duplicate
    assert set(analyse(rows)) == {"duplicate_timestamps"}


@VARIANTS
def test_conflicting_duplicates(interval):
    rows = clean(interval)
    original = rows[midday(rows, interval)]
    rows.append({**original, "kwh": original["kwh"] + 5})
    assert set(analyse(rows)) == {"conflicting_duplicates"}
