"""Register readings → per-reading consumption, window summaries and hour/day buckets."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from itertools import pairwise

import pytest

from urja_api.domain.normalize import IST, RawReading, readings_from_portal
from urja_api.domain.readings import (
    CONFLICTING_DUPLICATE,
    DUPLICATE,
    StoredReading,
    build_readings,
    consumption_buckets,
    detect_interval_minutes,
    merge_duplicates,
    register_span,
    rises,
)


def at(day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 6, day, hour, minute, tzinfo=IST)


def kwh(rows: list[StoredReading], ts: datetime) -> float:
    return next(r.register_kwh for r in rows if r.timestamp == ts)


def kvah(rows: list[StoredReading], ts: datetime) -> float:
    return next(r.register_kvah for r in rows if r.timestamp == ts)


def without(rows: list[StoredReading], *times: datetime) -> list[StoredReading]:
    return [r for r in rows if r.timestamp not in times]


# ----------------------------------------------------------------------------- duplicates


def test_blank_duplicate_row_is_merged_and_flagged(energy):
    raw = readings_from_portal(energy["J100089"])
    merged, duplicates = merge_duplicates(raw)
    assert (len(raw), len(merged)) == (31, 30)
    assert duplicates == {at(30): DUPLICATE}
    assert merged[-1] == RawReading(at(30), 12732.34, 13750.93, 229.0)


def test_a_duplicate_fills_blanks_from_either_row():
    merged, duplicates = merge_duplicates([RawReading(at(1), None, 108.0, None), RawReading(at(1), 100.0, None, 230)])
    assert merged == [RawReading(at(1), 100.0, 108.0, 230)]
    assert duplicates == {at(1): DUPLICATE}


def test_conflicting_duplicate_keeps_the_first_value_and_is_flagged():
    rows = [
        RawReading(at(2), 124.0, 133.92, 231.0),
        RawReading(at(1), 100.0, 108.0, 230.0),
        RawReading(at(1), 101.5, 108.0, None),
    ]
    merged, duplicates = merge_duplicates(rows)
    assert merged == [RawReading(at(1), 100.0, 108.0, 230.0), rows[0]]
    assert duplicates == {at(1): CONFLICTING_DUPLICATE}


def test_interval_detection(stored):
    assert detect_interval_minutes([r.timestamp for r in stored("J100000")]) == 30
    assert detect_interval_minutes([r.timestamp for r in stored("J100089")]) == 1440
    assert detect_interval_minutes([at(1), at(1), at(2)]) == 1440
    assert detect_interval_minutes([at(1)]) is None


# ----------------------------------------------------------------------------- readings


def test_clean_daily_series(stored):
    rows = stored("J100040")
    readings, summary = build_readings(rows, 1440)
    assert readings[0].consumption_kwh is None
    assert [r.consumption_kwh for r in readings[1:]] == [
        round(b.register_kwh - a.register_kwh, 2) for a, b in pairwise(rows)
    ]
    assert all(r.flags == [] for r in readings)
    assert summary.count == 30
    assert summary.interval_minutes == 1440
    assert summary.consumption_kwh == round(rows[-1].register_kwh - rows[0].register_kwh, 2)
    assert summary.average_power_factor == pytest.approx(
        (rows[-1].register_kwh - rows[0].register_kwh) / (rows[-1].register_kvah - rows[0].register_kvah), abs=1e-4
    )
    assert (summary.min_voltage_v, summary.max_voltage_v) == (
        min(r.voltage_v for r in rows),
        max(r.voltage_v for r in rows),
    )
    assert (summary.missing_intervals, summary.flags) == (0, {})


def test_flags_and_consumption_around_faults(stored):
    rows = stored("J100000")[:10]  # 01/06 00:00 .. 04:30, every 30 min
    rows[2] = replace(rows[2], voltage_v=None)
    rows[4] = replace(rows[4], register_kwh=None, register_kvah=None)
    rows[6] = replace(rows[6], register_kwh=rows[5].register_kwh - 5)  # the register goes down
    del rows[8]  # 04:00 is missing
    readings, summary = build_readings(rows, 30)

    assert [r.flags for r in readings] == [
        [],
        [],
        ["missing_voltage"],
        [],
        ["missing_kwh", "missing_kvah"],
        [],
        ["register_decrease"],
        [],
        ["gap_before"],
    ]
    assert readings[2].consumption_kwh == round(rows[2].register_kwh - rows[1].register_kwh, 2)
    assert [r.consumption_kwh for r in readings[4:7]] == [None, None, None]
    assert readings[8].consumption_kwh == round(rows[8].register_kwh - rows[7].register_kwh, 2)  # spans the gap
    assert summary.missing_intervals == 1
    assert summary.flags == {
        "gap_before": 1,
        "missing_kvah": 1,
        "missing_kwh": 1,
        "missing_voltage": 1,
        "register_decrease": 1,
    }


def test_duplicate_flags(stored):
    readings, summary = build_readings(stored("J100089"), 1440)
    assert readings[-1].flags == ["duplicate"]
    assert summary.flags == {"duplicate": 1}
    conflicting = replace(stored("J100089")[0], duplicate=CONFLICTING_DUPLICATE)
    assert build_readings([conflicting], 1440)[0][0].flags == ["conflicting_duplicate"]


def test_missing_intervals_count_every_absent_reading(stored):
    rows = without(stored("J100000")[:10], at(1, 1), at(1, 1, 30), at(1, 2))  # one gap, three readings
    _, summary = build_readings(rows, 30)
    assert (summary.missing_intervals, summary.flags) == (3, {"gap_before": 1})


def test_window_total_is_the_register_difference_despite_a_blank_value(stored):
    # ReadingsSummary.consumption_kwh is documented as the register difference across the
    # window; one blank register value in the middle must not drop the energy around it.
    rows = stored("J100000")[:10]
    rows[4] = replace(rows[4], register_kwh=None)
    _, summary = build_readings(rows, 30)
    assert summary.consumption_kwh == round(rows[-1].register_kwh - rows[0].register_kwh, 2)


# ----------------------------------------------------------------------------- the high-water mark


def half_hourly(values: list[float | None]) -> list[StoredReading]:
    start = at(1)
    return [
        StoredReading(start + timedelta(minutes=30 * i), v, None if v is None else v * 1.08, 230.0)
        for i, v in enumerate(values)
    ]


def test_rises_are_measured_against_the_highest_value_so_far():
    rows = half_hourly([1000.0, 1001.0, 1000.98, None, 1002.0, 1003.0])
    # The 0.02 dip is rounding slack (0.0); energy counts again once the register passes 1001.
    assert [rise for _, _, rise in rises(rows, "register_kwh")] == [1.0, 0.0, 1.0, 1.0]
    span = register_span(rows, "register_kwh")
    assert (span.total, span.decreased) == (3.0, False)  # exactly R(last) - R(first)


def test_read_noise_creates_no_energy():
    rows = half_hourly([1000.0, 999.99] * 24 + [1000.0])  # a register that never really moves
    readings, summary = build_readings(rows, 30)
    assert summary.consumption_kwh == 0.0 and summary.flags == {}
    assert {r.consumption_kwh for r in readings[1:]} == {0.0}
    [day] = consumption_buckets(rows, "day", at(1), at(2))
    assert (day.consumption_kwh, day.complete) == (0.0, True)


def test_a_register_creeping_backwards_is_a_decrease_everywhere():
    rows = half_hourly([5000.0 - 0.02 * i for i in range(49)])  # each step within the slack, 0.96 kWh in all
    readings, summary = build_readings(rows, 30)
    drops = [r.timestamp for r in readings if "register_decrease" in r.flags]
    assert len(drops) == 24 and summary.flags == {"register_decrease": 24}
    assert all(r.consumption_kwh is None for r in readings if "register_decrease" in r.flags)
    assert summary.consumption_kwh is None  # "it went down", not "0 kWh used"
    span = register_span(rows, "register_kwh")
    assert (span.total, span.decreased) == (0.0, True)
    [day] = consumption_buckets(rows, "day", at(1), at(2))
    assert day.complete is False  # a day in which the register went down is never "complete"


def test_a_window_is_judged_against_the_whole_series():
    rows = half_hourly([1000.0, 1000.02, 1000.0, 1000.0, 999.99, 999.99, 1000.5])
    window = rows[2:]  # starts after the peak: on its own, the fall to 999.99 would look like noise
    readings, summary = build_readings(window, 30, history=rows)
    assert [r.flags for r in readings] == [[], [], ["register_decrease"], [], []]
    assert [r.consumption_kwh for r in readings] == [None, 0.0, None, 0.0, 0.51]
    assert summary.consumption_kwh is None
    alone, _ = build_readings(window, 30)  # without the history it cannot know
    assert not any(r.flags for r in alone)


def test_apparent_energy_needs_the_same_stretch_of_the_bucket_not_just_as_much_of_it():
    rows = half_hourly([1000.0 + i for i in range(5)])  # 00:00 .. 02:00, kVAh = 1.08 x kWh
    rows[0] = replace(rows[0], register_kvah=None)  # kVAh is measured over 00:30-01:00...
    rows[2] = replace(rows[2], register_kwh=None)  # ...and kWh over 00:00-00:30: equally long, different halves
    first, second = consumption_buckets(rows, "hour", at(1), at(1, 1, 59))
    assert (first.consumption_kwh, first.apparent_kvah, first.power_factor) == (1.0, None, None)
    assert second.apparent_kvah is None  # there the two registers cover different halves as well


def test_a_windowed_span_agrees_with_the_buckets_built_from_the_same_rows(stored):
    rows = stored("J100000")  # half-hourly, 1-5 June
    noisy = list(rows)
    i = next(i for i, r in enumerate(rows) if r.timestamp == at(3))
    noisy[i] = replace(rows[i], register_kwh=rows[i - 1].register_kwh - 0.01)  # the window's first reading dips
    buckets = consumption_buckets(noisy, "day", at(3), at(4, 23, 59))
    span = register_span(noisy, "register_kwh", buckets[0].start, buckets[-1].end)
    assert span.total == pytest.approx(sum(b.consumption_kwh for b in buckets), abs=0.001)


# ----------------------------------------------------------------------------- buckets


def test_daily_buckets(stored):
    rows = stored("J100040")  # daily readings at 00:00, 1 to 30 June
    buckets = consumption_buckets(rows, "day", at(1), at(30, 23, 59))
    assert len(buckets) == 30
    for bucket, (a, b) in zip(buckets[:-1], pairwise(rows), strict=True):
        assert (bucket.start, bucket.end) == (a.timestamp, b.timestamp)
        assert bucket.complete
        assert bucket.coverage == 1.0
        assert bucket.consumption_kwh == round(b.register_kwh - a.register_kwh, 2)
        assert bucket.apparent_kvah == round(b.register_kvah - a.register_kvah, 2)
        assert bucket.power_factor == pytest.approx(bucket.consumption_kwh / bucket.apparent_kvah, abs=1e-4)
    # Nothing on 1 July closes 30 June: unknown, not guessed.
    last = buckets[-1]
    assert (last.start, last.consumption_kwh, last.apparent_kvah, last.power_factor) == (at(30), None, None, None)
    assert (last.coverage, last.complete) == (0.0, False)


def test_half_hourly_day_without_its_closing_midnight_is_partial(stored):
    rows = stored("J100000")  # 01/06 00:00 .. 05/06 23:30
    buckets = consumption_buckets(rows, "day", at(1), at(5, 23, 30))
    assert [b.complete for b in buckets] == [True, True, True, True, False]
    assert [b.coverage for b in buckets] == [1.0, 1.0, 1.0, 1.0, round(47 / 48, 4)]
    assert buckets[-1].consumption_kwh == round(kwh(rows, at(5, 23, 30)) - kwh(rows, at(5)), 2)


def test_a_gap_inside_a_day_leaves_it_complete(stored):
    rows = without(stored("J100000"), at(2, 10), at(2, 10, 30), at(2, 11), at(2, 11, 30))
    (day,) = consumption_buckets(rows, "day", at(2), at(2, 23, 59))
    assert day.complete
    assert day.coverage == 1.0
    assert day.consumption_kwh == round(kwh(rows, at(3)) - kwh(rows, at(2)), 2)  # registers are cumulative


def test_an_interval_across_a_bucket_boundary_counts_for_neither(stored):
    rows = without(stored("J100000"), at(2))  # 01/06 23:30 → 02/06 00:30 spans midnight
    day1, day2 = consumption_buckets(rows, "day", at(1), at(2, 23, 59))
    assert not day1.complete
    assert not day2.complete
    assert day1.consumption_kwh == round(kwh(rows, at(1, 23, 30)) - kwh(rows, at(1)), 2)
    assert day2.consumption_kwh == round(kwh(rows, at(3)) - kwh(rows, at(2, 0, 30)), 2)
    assert day1.coverage == day2.coverage == round(47 / 48, 4)


def test_a_register_decrease_makes_the_day_incomplete(stored):
    rows = stored("J100040")
    rows[3] = replace(rows[3], register_kwh=rows[2].register_kwh - 1)  # 04/06 below 03/06
    buckets = consumption_buckets(rows, "day", at(1), at(5))
    assert [b.complete for b in buckets] == [True, True, False, True]


def test_hourly_buckets(stored):
    rows = stored("J100000")
    buckets = consumption_buckets(rows, "hour", at(2, 10), at(2, 13))
    assert [b.start for b in buckets] == [at(2, 10), at(2, 11), at(2, 12)]
    for bucket in buckets:
        assert bucket.complete
        assert bucket.coverage == 1.0
        assert bucket.consumption_kwh == round(kwh(rows, bucket.end) - kwh(rows, bucket.start), 2)
        assert bucket.apparent_kvah == round(kvah(rows, bucket.end) - kvah(rows, bucket.start), 2)
        assert bucket.power_factor is None  # hourly ratios of 0.01-resolution registers are noise


def test_a_blank_kvah_value_does_not_distort_apparent_energy(stored):
    rows = stored("J100000")
    noon = next(i for i, r in enumerate(rows) if r.timestamp == at(2, 12))
    rows[noon] = replace(rows[noon], register_kvah=None)
    (day,) = consumption_buckets(rows, "day", at(2), at(2, 23, 59))
    true_kvah = round(kvah(rows, at(3)) - kvah(rows, at(2)), 2)
    assert day.complete
    # Unknown is acceptable; an undercount (and the inflated power factor it implies) is not.
    assert day.apparent_kvah in (None, true_kvah)
    assert day.power_factor in (None, round(day.consumption_kwh / true_kvah, 4))
