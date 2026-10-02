"""Translate portal payloads into internal records.

Pure functions, no I/O. Each portal view gets its own adapter, but they all produce the
same `MeterRecord`, so the rest of the service doesn't care whether data came from the
bulk export or from crawling detail pages.

Portal conventions handled here:

* numbers arrive as strings (``"48438.74"``, ``"226"``); empty strings mean "missing";
* timestamps are ``DD/MM/YYYY HH:MM`` with no zone. They are read as IST (UTC+05:30):
  the utility is in Jaipur and India has had a single, DST-free offset since 1945;
* network levels are ``{"name", "code"}`` in the export but ``"Name (CODE)"`` strings on
  the detail page, where either part can be blank;
* the detail nameplate is either a ``[{parameterName, parameterValue}]`` list ("legacy")
  or a JSON *string* holding ``installed_meter`` with PascalCase keys ("v2").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import InstallationType, Location, MeterStatus, NetworkLevel, Phase

IST = timezone(timedelta(hours=5, minutes=30), "IST")

_EXPORT_LEVEL_KEYS: dict[NetworkLevel, str] = {
    NetworkLevel.zone: "zone",
    NetworkLevel.circle: "circle",
    NetworkLevel.division: "division",
    NetworkLevel.subdivision: "subdivision",
    NetworkLevel.substation: "substation",
    NetworkLevel.feeder: "feeder",
    NetworkLevel.transformer: "dt",
}
_DETAIL_LEVEL_KEYS: dict[NetworkLevel, str] = {
    NetworkLevel.zone: "Zone",
    NetworkLevel.circle: "Circle",
    NetworkLevel.division: "Division",
    NetworkLevel.subdivision: "Subdivision",
    NetworkLevel.substation: "Sub Station",
    NetworkLevel.feeder: "Feeder",
    NetworkLevel.transformer: "DT",
}
# Nameplate keys as they appear in the two detail formats, mapped to our field names.
_LEGACY_NAMEPLATE = {
    "Meter ID": "meter_id",
    "Serial No": "serial_number",
    "Make": "make",
    "Phase Type": "phase",
    "Installation Status": "status",
    "Installation Type": "installation_type",
}
_V2_NAMEPLATE = {
    "MeterId": "meter_id",
    "SerialNo": "serial_number",
    "Make": "make",
    "PhaseType": "phase",
    "InstallationStatus": "status",
    "InstallationType": "installation_type",
}
_STATUS = {
    "installed": MeterStatus.installed,
    "faulty": MeterStatus.faulty,
    "decommissioned": MeterStatus.decommissioned,
}
_PHASE = {"single": Phase.single, "three": Phase.three}
_INSTALLATION = {"whole current": InstallationType.whole_current, "ct operated": InstallationType.ct_operated}
_CODE_LABEL = re.compile(r"^(?P<name>.*?)\s*\((?P<code>[^()]*)\)\s*$")
_TIMESTAMP = re.compile(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})\s*$")


@dataclass(frozen=True)
class ReportedNode:
    """A network level as one portal record reported it (either part may be missing)."""

    code: str | None
    name: str | None


@dataclass
class MeterRecord:
    meter_id: str
    serial_number: str
    make: str
    phase: Phase
    status: MeterStatus
    installation_type: InstallationType
    transformer_code: str
    location: Location | None
    reported_path: dict[NetworkLevel, ReportedNode]
    vocabulary_issues: list[tuple[str, str]] = field(default_factory=list)  # (field, raw value)


@dataclass(frozen=True)
class TransformerRecord:
    code: str
    name: str
    feeder_code: str
    capacity_kva: float | None


@dataclass(frozen=True)
class RawReading:
    timestamp: datetime
    energy_kwh: float | None
    energy_kvah: float | None
    voltage_v: float | None


class NormalizationError(ValueError):
    """A portal record is missing something we cannot do without (e.g. the meter id)."""


# ----------------------------------------------------------------------------- scalars


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_portal_timestamp(value: Any) -> datetime:
    """``"23/06/2026 23:30"`` → ``2026-06-23T23:30:00+05:30``."""
    match = _TIMESTAMP.match(value) if isinstance(value, str) else None
    if not match:
        raise NormalizationError(f"unrecognised portal timestamp: {value!r}")
    day, month, year, hour, minute = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute, tzinfo=IST)
    except ValueError as exc:  # right shape, impossible value: "31/06/2026 00:00"
        raise NormalizationError(f"invalid portal timestamp: {value!r}") from exc


def parse_code_label(value: Any) -> ReportedNode:
    """``"Jaipur Zone 1 (Z-01)"`` → name/code; tolerates ``"Circle 6 ()"`` and ``" (C-01)"``."""
    text = clean_text(value)
    if text is None:
        return ReportedNode(code=None, name=None)
    match = _CODE_LABEL.match(text)
    if not match:
        return ReportedNode(code=None, name=text)
    return ReportedNode(code=clean_text(match["code"]), name=clean_text(match["name"]))


def _vocab(mapping: dict[str, Any], raw: Any, fallback: Any, field_name: str, issues: list[tuple[str, str]]) -> Any:
    key = (clean_text(raw) or "").lower()
    if key in mapping:
        return mapping[key]
    issues.append((field_name, str(raw)))
    return fallback


def _location(lat: Any, lng: Any) -> Location | None:
    latitude, longitude = parse_number(lat), parse_number(lng)
    if latitude is None or longitude is None or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    if latitude == 0 and longitude == 0:
        return None
    return Location(latitude=latitude, longitude=longitude)


def _nameplate_record(
    plate: dict[str, Any],
    path: dict[NetworkLevel, ReportedNode],
    transformer_code: Any,
    location: Location | None,
) -> MeterRecord:
    meter_id = clean_text(plate.get("meter_id"))
    if not meter_id:
        raise NormalizationError(f"record without a meter id: {plate!r}")
    issues: list[tuple[str, str]] = []
    dt_code = clean_text(transformer_code) or path[NetworkLevel.transformer].code
    if not dt_code:
        raise NormalizationError(f"meter {meter_id} has no transformer code")
    return MeterRecord(
        meter_id=meter_id,
        serial_number=clean_text(plate.get("serial_number")) or "",
        make=clean_text(plate.get("make")) or "",
        phase=_vocab(_PHASE, plate.get("phase"), Phase.unknown, "phase", issues),
        status=_vocab(_STATUS, plate.get("status"), MeterStatus.unknown, "status", issues),
        installation_type=_vocab(
            _INSTALLATION, plate.get("installation_type"), InstallationType.unknown, "installation_type", issues
        ),
        transformer_code=dt_code,
        location=location,
        reported_path=path,
        vocabulary_issues=issues,
    )


# ----------------------------------------------------------------------------- meters


def meter_from_export(record: dict[str, Any]) -> MeterRecord:
    """One record of ``GET /portal/export``."""
    hierarchy = record.get("hierarchy") or {}
    path = {
        level: ReportedNode(
            code=clean_text((hierarchy.get(key) or {}).get("code")),
            name=clean_text((hierarchy.get(key) or {}).get("name")),
        )
        for level, key in _EXPORT_LEVEL_KEYS.items()
    }
    geo = record.get("geo") or {}
    plate = {
        "meter_id": record.get("meterId"),
        "serial_number": record.get("serialNo"),
        "make": record.get("make"),
        "phase": record.get("phaseType"),
        "status": record.get("installStatus"),
        "installation_type": record.get("installType"),
    }
    return _nameplate_record(plate, path, record.get("dtCode"), _location(geo.get("lat"), geo.get("lng")))


def nameplate_from_detail(detail: dict[str, Any]) -> dict[str, Any]:
    """Nameplate fields from a detail page, whichever of the two formats it uses."""
    if "classData" in detail:
        try:
            installed = json.loads(detail["classData"]).get("installed_meter") or {}
        except (TypeError, ValueError) as exc:
            raise NormalizationError(f"unparseable classData: {detail['classData']!r}") from exc
        return {ours: installed.get(theirs) for theirs, ours in _V2_NAMEPLATE.items()}
    params = {
        clean_text(p.get("parameterName")): p.get("parameterValue")
        for p in detail.get("data") or []
        if isinstance(p, dict)
    }
    return {ours: params.get(theirs) for theirs, ours in _LEGACY_NAMEPLATE.items()}


def meter_from_detail_page(page: dict[str, Any]) -> MeterRecord:
    """A decoded ``/meters/{id}/__data.json`` page: the crawl fallback. It carries no coordinates."""
    plate = nameplate_from_detail(page.get("detail") or {})
    plate["meter_id"] = plate.get("meter_id") or page.get("meterId")
    hierarchy = page.get("hierarchy") or {}
    path = {level: parse_code_label(hierarchy.get(key)) for level, key in _DETAIL_LEVEL_KEYS.items()}
    return _nameplate_record(plate, path, None, None)


def transformer_from_portal(row: dict[str, Any]) -> TransformerRecord:
    code = clean_text(row.get("code"))
    if not code:
        raise NormalizationError(f"transformer without a code: {row!r}")
    return TransformerRecord(
        code=code,
        name=clean_text(row.get("name")) or code,
        feeder_code=clean_text(row.get("feederCode")) or "",
        capacity_kva=parse_number(row.get("capacityKva")),
    )


# ----------------------------------------------------------------------------- readings


def readings_from_portal(rows: list[dict[str, Any]]) -> list[RawReading]:
    """``/energy`` rows → typed readings, sorted by time. Rows with a bad timestamp are dropped."""
    readings = []
    for row in rows:
        try:
            ts = parse_portal_timestamp(row.get("timestamp", ""))
        except NormalizationError:
            continue
        readings.append(
            RawReading(
                timestamp=ts,
                energy_kwh=parse_number(row.get("kwh")),
                energy_kvah=parse_number(row.get("kvah")),
                voltage_v=parse_number(row.get("voltR")),
            )
        )
    readings.sort(key=lambda r: r.timestamp)
    return readings
