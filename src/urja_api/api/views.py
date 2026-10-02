"""Converting store rows into API models, and parsing shared query parameters."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from ..domain.anomalies import Anomaly, ranked
from ..domain.models import (
    AnomalyOut,
    DataIssue,
    InstallationType,
    Location,
    Meter,
    MeterListItem,
    MeterStatus,
    MeterSummary,
    NetworkPath,
    Phase,
    ReadingsCoverage,
    TransformerSummary,
)
from ..domain.normalize import IST
from .errors import ApiProblem

DEFAULT_WINDOW = timedelta(days=7)
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# In a URL an unencoded "+05:30" arrives as " 05:30"; nothing else can end a datetime that way.
SPACE_FOR_PLUS = re.compile(r" (\d{2}(?::?\d{2}(?::?\d{2})?)?)$")
MIN_INSTANT = datetime(2000, 1, 1, tzinfo=IST)
MAX_INSTANT = datetime(2100, 12, 31, 23, 59, 59, 999999, tzinfo=IST)


def normalize_meter_id(raw: str) -> str:
    """The portal's ids are upper case (J100000) and case-sensitive; we accept any case."""
    return raw.strip().upper()


def meter_summary(row: sqlite3.Row) -> MeterSummary:
    return MeterSummary(
        meter_id=row["meter_id"],
        serial_number=row["serial_number"],
        make=row["make"],
        phase=Phase(row["phase"]),
        status=MeterStatus(row["status"]),
        installation_type=InstallationType(row["installation_type"]),
        transformer_code=row["transformer_code"],
        feeder_code=row["feeder_code"],
        location=_location(row),
    )


def meter_list_item(row: sqlite3.Row, distance_km: float | None) -> MeterListItem:
    return MeterListItem(
        **meter_summary(row).model_dump(),
        data_issue_count=row["issue_count"],
        reading_interval_minutes=row["interval_minutes"],
        distance_km=distance_km,
    )


def meter_detail(row: sqlite3.Row, transformer: sqlite3.Row | None) -> Meter:
    summary = meter_summary(row)
    return Meter(
        **summary.model_dump(),
        network=NetworkPath.model_validate_json(row["network_json"]),
        transformer=transformer_summary(transformer) if transformer else None,
        data_issues=[DataIssue.model_validate(i) for i in json.loads(row["issues_json"])],
        readings_coverage=_coverage(row),
        synced_at=datetime.fromisoformat(row["synced_at"]),
    )


def _coverage(row: sqlite3.Row) -> ReadingsCoverage | None:
    if row["readings_fetched_at"] is None:
        return None
    return ReadingsCoverage(
        interval_minutes=row["interval_minutes"],
        count=row["reading_count"],
        first_reading_at=_iso(row["first_ts"]),
        last_reading_at=_iso(row["last_ts"]),
        fetched_at=datetime.fromisoformat(row["readings_fetched_at"]),
    )


def _iso(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def transformer_summary(row: sqlite3.Row) -> TransformerSummary:
    return TransformerSummary(
        code=row["code"], name=row["name"], feeder_code=row["feeder_code"], capacity_kva=row["capacity_kva"]
    )


def anomalies_out(anomalies: list[Anomaly]) -> list[AnomalyOut]:
    """Most severe first, then by rule."""
    return [AnomalyOut.model_validate(a, from_attributes=True) for a in ranked(anomalies)]


def _location(row: sqlite3.Row) -> Location | None:
    if row["latitude"] is None or row["longitude"] is None:
        return None
    return Location(latitude=row["latitude"], longitude=row["longitude"])


# ----------------------------------------------------------------------------- query parsing


def parse_point(raw: str | None, name: str) -> tuple[float, float] | None:
    if raw is None:
        return None
    lat, lng = _floats(raw, 2, name)
    _check_coordinates(name, lat, lng)
    return lat, lng


def parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    """GeoJSON order: min_lng,min_lat,max_lng,max_lat → (min_lat, min_lng, max_lat, max_lng)."""
    if raw is None:
        return None
    min_lng, min_lat, max_lng, max_lat = _floats(raw, 4, "bbox")
    _check_coordinates("bbox", min_lat, min_lng)
    _check_coordinates("bbox", max_lat, max_lng)
    if min_lat > max_lat or min_lng > max_lng:
        raise _invalid("bbox", "expected min_lng,min_lat,max_lng,max_lat with min <= max")
    return min_lat, min_lng, max_lat, max_lng


def _check_coordinates(name: str, lat: float, lng: float) -> None:
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):  # also rejects NaN and infinities
        raise _invalid(name, "latitude must be within ±90 and longitude within ±180")


def _floats(raw: str, count: int, name: str) -> list[float]:
    try:
        values = [float(p) for p in raw.split(",")]
    except ValueError:
        values = []
    if len(values) != count:
        raise _invalid(name, f"expected {count} comma-separated numbers")
    return values


def _invalid(name: str, message: str) -> ApiProblem:
    return ApiProblem(
        422,
        "validation_error",
        f"Invalid query parameter {name!r}: {message}.",
        errors=[{"location": ["query", name], "message": message, "type": "value_error"}],
    )


@dataclass(frozen=True)
class Window:
    start: datetime | None
    end: datetime | None  # inclusive

    def __contains__(self, ts: datetime) -> bool:
        return (self.start is None or ts >= self.start) and (self.end is None or ts <= self.end)


@dataclass(frozen=True)
class WindowParams:
    """`from` / `to` as given, parsed and validated but not yet defaulted."""

    start: datetime | None
    end: datetime | None
    end_date: date | None  # set when `to` was a bare date


def parse_window(raw_from: str | None, raw_to: str | None) -> WindowParams:
    """Parse `from` / `to`: `YYYY-MM-DD` or an ISO 8601 datetime, between 2000 and 2100.

    Datetimes in any offset are converted to IST (naive ones are read as IST), so days and
    hours always follow the IST calendar. A bare date as `to` covers that whole day, which
    matches how the portal treats it.
    """
    start, _ = _parse_instant(raw_from, "from", end_of_day=False)
    end, end_date = _parse_instant(raw_to, "to", end_of_day=True)
    if start and end and start > end:
        raise _invalid("from", "'from' must not be after 'to'")
    return WindowParams(start, end, end_date)


def _parse_instant(raw: str | None, name: str, *, end_of_day: bool) -> tuple[datetime | None, date | None]:
    if raw is None or raw == "":
        return None, None
    try:
        if DATE_ONLY.match(raw):
            day = date.fromisoformat(raw)
            value = datetime.combine(day, time.max if end_of_day else time.min, tzinfo=IST)
        elif "T" in raw:
            value, day = datetime.fromisoformat(SPACE_FOR_PLUS.sub(r"+\1", raw)), None
            if value.tzinfo is None:
                value = value.replace(tzinfo=IST)
        else:
            raise ValueError(raw)
    except ValueError as exc:
        raise _invalid(name, "expected YYYY-MM-DD or an ISO 8601 datetime such as 2026-06-01T10:00:00+05:30") from exc
    # Checked before converting: astimezone() overflows for instants near year 1 or 9999.
    if not MIN_INSTANT <= value <= MAX_INSTANT:
        raise _invalid(name, "dates must fall between 2000-01-01 and 2100-12-31")
    return value.astimezone(IST), day


def resolve_window(params: WindowParams, latest: datetime | None) -> Window:
    """Fill in the bounds that weren't given.

    Neither bound: the 7 days up to the meter's latest reading (the portal's own default,
    which is relative to the *data*, not to today). Only `from`: up to the latest reading.
    Only `to`: the 7 days ending then; a bare date means 7 whole days, `to` included.
    """
    start, end = params.start, params.end
    if start is None and end is None:
        return Window(None, None) if latest is None else Window(latest - DEFAULT_WINDOW, latest)
    if start is None:
        assert end is not None
        if params.end_date is not None:
            start = datetime.combine(params.end_date - timedelta(days=6), time.min, tzinfo=IST)
        else:
            start = end - DEFAULT_WINDOW
        return Window(start, end)
    end = end or latest
    if end is None or end < start:  # nothing after `from`: an empty window, not an inverted one
        end = start
    return Window(start, end)
