"""Turning cumulative register readings into consumption.

The portal returns *register* values: kWh and kVAh counters that only go up, sampled
every 30 minutes (40 meters) or once a day at 00:00 (the rest), with 0.01 resolution.
(The portal's own UI labels this column "Consumption"; summing it overstates usage by
orders of magnitude.) Consumption is the difference between two register values, so:

* a reading's `consumption_kwh` is its rise since the previous reading in the window
  (null for the first one, when either value is missing, or when the register goes
  *down*, which would mean a reset, rollover or meter swap and is flagged instead);
* consumption over a span [a, b] is R(b) − R(a). A day bucket needs the 00:00 reading
  of that day *and* of the next day. Without both it's marked incomplete, and it isn't
  guessed.

Every rise is measured against the register's **highest value so far** (`rises`). Values
are rounded to 0.01, so a reading up to 0.02 below that mark is read as "no consumption",
and energy counts again only once the register passes the mark: a noisy read can't create
energy, and the steps always add up to R(last) − R(first). Further below the mark, the
register has really gone down: the reading is flagged, and counting restarts from the new
level. Readings, consumption buckets and the anomaly rules all go through this one helper.

Duplicate timestamps do occur: five meters repeat 30/06 00:00 with a blank kWh and
voltage. Rows sharing a timestamp are merged field by field (a present value beats a
blank one). If two *different* values collide, the first is kept and the reading is
flagged `conflicting_duplicate`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any, Literal

from .models import ConsumptionBucket, Reading, ReadingFlag, ReadingsSummary
from .normalize import IST, RawReading

RESOLUTION_DP = 2  # registers are reported to 0.01 kWh / kVAh
REGISTER_TOLERANCE = 0.02  # two register steps of rounding slack
# Below this much apparent energy, rounding to 0.01 distorts kWh/kVAh by 1 % or more.
MIN_KVAH_FOR_POWER_FACTOR = 1.0

NOT_DUPLICATED, DUPLICATE, CONFLICTING_DUPLICATE = 0, 1, 2


def merge_duplicates(readings: Sequence[RawReading]) -> tuple[list[RawReading], dict[datetime, int]]:
    """Collapse rows that share a timestamp. Returns the merged series and, for each
    affected timestamp, `DUPLICATE` or `CONFLICTING_DUPLICATE`."""
    groups: dict[datetime, list[RawReading]] = defaultdict(list)
    for r in readings:
        groups[r.timestamp].append(r)
    merged: list[RawReading] = []
    status: dict[datetime, int] = {}
    for ts in sorted(groups):
        rows = groups[ts]
        if len(rows) == 1:
            merged.append(rows[0])
            continue
        fields: dict[str, float | None] = {}
        conflict = False
        for name in ("energy_kwh", "energy_kvah", "voltage_v"):
            values = [getattr(r, name) for r in rows if getattr(r, name) is not None]
            fields[name] = values[0] if values else None
            conflict |= len(set(values)) > 1
        merged.append(RawReading(timestamp=ts, **fields))
        status[ts] = CONFLICTING_DUPLICATE if conflict else DUPLICATE
    return merged, status


def detect_interval_minutes(timestamps: Sequence[datetime]) -> int | None:
    """The most common spacing between consecutive readings, in minutes."""
    deltas = Counter(int((b - a).total_seconds() // 60) for a, b in pairwise(timestamps) if b > a)
    if not deltas:
        return None
    return min(deltas, key=lambda d: (-deltas[d], d))


@dataclass(frozen=True)
class StoredReading:
    """A cached reading as read back from the store."""

    timestamp: datetime
    register_kwh: float | None
    register_kvah: float | None
    voltage_v: float | None
    duplicate: int = NOT_DUPLICATED

    @classmethod
    def from_row(cls, row: Any) -> StoredReading:
        return cls(
            timestamp=datetime.fromisoformat(row["ts"]),
            register_kwh=row["kwh"],
            register_kvah=row["kvah"],
            voltage_v=row["voltage"],
            duplicate=int(row["duplicate"]),
        )


Register = Literal["register_kwh", "register_kvah"]


def rises(rows: Sequence[StoredReading], attr: Register) -> Iterator[tuple[StoredReading, StoredReading, float | None]]:
    """For each pair of consecutive readings that carry a value: the later reading's rise
    above the register's highest value so far. 0.0 while it sits within the rounding
    tolerance below that mark; None when it fell further, i.e. the register went down
    (counting then restarts from the new level)."""
    peak: float | None = None
    previous: StoredReading | None = None
    for row in rows:
        value = getattr(row, attr)
        if value is None:
            continue
        if previous is None or peak is None:
            peak = value
        else:
            rise: float | None
            if round(peak - value, RESOLUTION_DP) > REGISTER_TOLERANCE:
                rise, peak = None, value
            elif value <= peak:
                rise = 0.0
            else:
                rise, peak = round(value - peak, RESOLUTION_DP), value
            yield previous, row, rise
        previous = row


def build_readings(
    rows: Sequence[StoredReading], interval_minutes: int | None, history: Sequence[StoredReading] | None = None
) -> tuple[list[Reading], ReadingsSummary]:
    """Readings for a window, with per-reading consumption and a summary.

    `history` is the whole series that `rows` is a window of. The register's high mark
    then comes from everything before the window too, so a reading is a decrease (or
    noise) here exactly when the consumption buckets and the anomaly rules say so.
    """
    series = rows if history is None else history
    readings: list[Reading] = []
    flags_count: Counter[ReadingFlag] = Counter()
    missing_intervals = 0
    gap = timedelta(minutes=interval_minutes) if interval_minutes else None
    rise_at = {b.timestamp: rise for _, b, rise in rises(series, "register_kwh")}

    for i, row in enumerate(rows):
        flags: list[ReadingFlag] = []
        consumption: float | None = None
        if row.register_kwh is None:
            flags.append("missing_kwh")
        if row.register_kvah is None:
            flags.append("missing_kvah")
        if row.voltage_v is None:
            flags.append("missing_voltage")
        if row.duplicate == DUPLICATE:
            flags.append("duplicate")
        elif row.duplicate == CONFLICTING_DUPLICATE:
            flags.append("conflicting_duplicate")
        rise = rise_at.get(row.timestamp, 0.0)
        if rise is None:
            flags.append("register_decrease")
        if i > 0:
            prev = rows[i - 1]
            if gap and row.timestamp - prev.timestamp > gap:
                flags.append("gap_before")
                missing_intervals += int((row.timestamp - prev.timestamp) / gap) - 1
            # Null when either value is missing: the rise would span more than this interval.
            if row.timestamp in rise_at and rise is not None and prev.register_kwh is not None:
                consumption = rise
        flags_count.update(flags)
        readings.append(
            Reading(
                timestamp=row.timestamp,
                register_kwh=row.register_kwh,
                register_kvah=row.register_kvah,
                voltage_v=row.voltage_v,
                consumption_kwh=consumption,
                flags=flags,
            )
        )

    window = (rows[0].timestamp, rows[-1].timestamp) if rows else (None, None)
    kwh = register_span(series, "register_kwh", *window) if rows else Span(None, None, None, False)
    kvah = register_span(series, "register_kvah", *window) if rows else Span(None, None, None, False)
    voltages = [r.voltage_v for r in rows if r.voltage_v is not None]
    summary = ReadingsSummary(
        count=len(rows),
        interval_minutes=interval_minutes,
        consumption_kwh=None if kwh.decreased else kwh.total,
        average_power_factor=power_factor(kwh, kvah),
        min_voltage_v=min(voltages) if voltages else None,
        max_voltage_v=max(voltages) if voltages else None,
        missing_intervals=missing_intervals,
        flags=dict(sorted(flags_count.items())),
    )
    return readings, summary


@dataclass(frozen=True)
class Span:
    """Energy recorded by one register between its first and last reading in a window."""

    total: float | None
    first: datetime | None
    last: datetime | None
    decreased: bool


def register_span(
    rows: Sequence[StoredReading], attr: Register, start: datetime | None = None, end: datetime | None = None
) -> Span:
    """Energy recorded between the first and last reading that carry a value: R(last) −
    R(first), skipping any drop. With `start`/`end`, only the intervals that lie inside
    them count, while the register's history before `start` still sets the high mark, so
    the result agrees with the buckets `consumption_buckets` builds from the same rows.

    Registers are cumulative, so a blank value in the middle loses nothing: each rise is
    measured between the neighbouring readings that do carry a value.
    """
    total, first, last, decreased = 0.0, None, None, False
    for a, b, rise in rises(rows, attr):
        if (start is not None and a.timestamp < start) or (end is not None and b.timestamp > end):
            continue
        first = first or a.timestamp
        last = b.timestamp
        if rise is None:
            decreased = True
        else:
            total += rise
    return Span(round(total, RESOLUTION_DP) if first is not None else None, first, last, decreased)


def power_factor(kwh: Span, kvah: Span) -> float | None:
    """kWh / kVAh, only when both registers measured exactly the same span and there is
    enough apparent energy for the 0.01 rounding not to dominate the ratio."""
    same_span = (kwh.first, kwh.last) == (kvah.first, kvah.last) and not (kwh.decreased or kvah.decreased)
    if not same_span or not kwh.total or not kvah.total or kvah.total < MIN_KVAH_FOR_POWER_FACTOR:
        return None
    return round(kwh.total / kvah.total, 4)


@dataclass
class _Buckets:
    sums: list[float]
    covered: list[timedelta]
    found: list[bool]
    decreased: list[bool]
    spans: list[tuple[datetime, datetime] | None]  # first counted interval's start, last one's end
    readings_at: set[datetime]


def _bucket_register(
    rows: Sequence[StoredReading], attr: Register, first: datetime, size: timedelta, count: int
) -> _Buckets:
    """Sum one register's rises per bucket. An interval between consecutive readings that
    carry a value belongs to the bucket it starts in, and counts only if it also ends by
    that bucket's end."""
    out = _Buckets([0.0] * count, [timedelta()] * count, [False] * count, [False] * count, [None] * count, set())
    out.readings_at = {r.timestamp for r in rows if getattr(r, attr) is not None}
    for a, b, rise in rises(rows, attr):
        index = (a.timestamp - first) // size
        if not 0 <= index < count or b.timestamp > first + (index + 1) * size:
            continue  # outside the window, or spans a bucket boundary
        if rise is None:
            out.decreased[index] = True
            continue
        out.sums[index] += rise
        out.covered[index] += b.timestamp - a.timestamp
        out.found[index] = True
        span = out.spans[index]
        out.spans[index] = (span[0] if span else a.timestamp, b.timestamp)
    return out


def consumption_buckets(
    rows: Sequence[StoredReading], granularity: str, start: datetime, end: datetime
) -> list[ConsumptionBucket]:
    """Energy per calendar hour/day (IST), from register values at the bucket boundaries.

    A bucket is `complete` when there are kWh readings exactly at both of its boundaries
    and the register never went down in between; `coverage` is the share of the bucket
    spanned by counted intervals. kWh and kVAh are bucketed independently, and apparent
    energy (and power factor) is only reported when the kVAh register covers exactly the
    same span as kWh: the same stretch of the bucket, not merely as much of it.
    """
    size = {"hour": timedelta(hours=1), "day": timedelta(days=1)}[granularity]
    first = bucket_floor(start, granularity)
    count = max(0, -(-(end - first) // size))  # ceil
    kwh = _bucket_register(rows, "register_kwh", first, size, count)
    kvah = _bucket_register(rows, "register_kvah", first, size, count)

    buckets = []
    for i in range(count):
        bucket_start = first + i * size
        bucket_end = bucket_start + size
        same_span = kvah.spans[i] == kwh.spans[i] and kvah.covered[i] == kwh.covered[i]
        apparent = kwh.found[i] and kvah.found[i] and same_span and not kvah.decreased[i]
        with_pf = apparent and kvah.sums[i] >= MIN_KVAH_FOR_POWER_FACTOR
        buckets.append(
            ConsumptionBucket(
                start=bucket_start,
                end=bucket_end,
                consumption_kwh=round(kwh.sums[i], RESOLUTION_DP) if kwh.found[i] else None,
                apparent_kvah=round(kvah.sums[i], RESOLUTION_DP) if apparent else None,
                power_factor=round(kwh.sums[i] / kvah.sums[i], 4) if with_pf else None,
                coverage=round(min(1.0, kwh.covered[i] / size), 4),
                complete=bucket_start in kwh.readings_at and bucket_end in kwh.readings_at and not kwh.decreased[i],
            )
        )
    return buckets


def bucket_floor(ts: datetime, granularity: str) -> datetime:
    """Start of the IST hour/day containing `ts` (aware values are converted to IST first)."""
    ts = ts.astimezone(IST)
    if granularity == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)
