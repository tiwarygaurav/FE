"""Portal payloads → internal records, on real records copied from the portal."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from urja_api.domain.models import InstallationType, Location, MeterStatus, NetworkLevel, Phase
from urja_api.domain.normalize import (
    IST,
    NormalizationError,
    RawReading,
    ReportedNode,
    TransformerRecord,
    meter_from_detail_page,
    meter_from_export,
    parse_code_label,
    parse_number,
    parse_portal_timestamp,
    readings_from_portal,
    transformer_from_portal,
)
from urja_api.portal.devalue import unflatten


def export_record(records: list[dict], meter_id: str) -> dict:
    return next(r for r in records if r["meterId"] == meter_id)


def decoded_page(raw: dict) -> dict:
    """The page node of a raw `__data.json` response, decoded."""
    return unflatten(raw["nodes"][-1]["data"])


# ----------------------------------------------------------------------------- export


def test_export_record(export_records):
    meter = meter_from_export(export_record(export_records, "J100000"))
    assert (meter.meter_id, meter.serial_number, meter.make) == ("J100000", "SE33962", "HPL")
    assert (meter.phase, meter.status, meter.installation_type) == (
        Phase.single,
        MeterStatus.decommissioned,
        InstallationType.whole_current,
    )
    assert meter.transformer_code == "DT-001"
    assert meter.location == Location(latitude=26.938961, longitude=75.830957)  # 15 decimals → 6
    assert meter.reported_path[NetworkLevel.zone] == ReportedNode(code="Z-01", name="Jaipur Zone 1")
    assert meter.reported_path[NetworkLevel.transformer] == ReportedNode(code="DT-001", name="Malviya Nagar DT 1")
    assert meter.vocabulary_issues == []


@pytest.mark.parametrize(
    ("meter_id", "level", "expected"),
    [
        ("J100011", NetworkLevel.circle, ReportedNode(code=None, name="Circle 6")),
        ("J100153", NetworkLevel.substation, ReportedNode(code=None, name="Substation 16")),
        ("J100162", NetworkLevel.feeder, ReportedNode(code="F-003", name=None)),
        ("J100218", NetworkLevel.circle, ReportedNode(code="C-01", name=None)),
    ],
)
def test_blank_export_values_become_none(export_records, meter_id, level, expected):
    assert meter_from_export(export_record(export_records, meter_id)).reported_path[level] == expected


def test_unknown_vocabulary_is_served_as_unknown_and_recorded(export_records):
    record = export_record(export_records, "J100000") | {
        "installStatus": "Removed",
        "phaseType": "two",
        "installType": "LT CT",
    }
    meter = meter_from_export(record)
    assert (meter.phase, meter.status, meter.installation_type) == (
        Phase.unknown,
        MeterStatus.unknown,
        InstallationType.unknown,
    )
    assert meter.vocabulary_issues == [("phase", "two"), ("status", "Removed"), ("installation_type", "LT CT")]


def test_vocabulary_ignores_case_and_whitespace(export_records):
    record = export_record(export_records, "J100004") | {"installStatus": "  FAULTY ", "installType": "ct operated"}
    meter = meter_from_export(record)
    assert (meter.status, meter.installation_type) == (MeterStatus.faulty, InstallationType.ct_operated)
    assert meter.vocabulary_issues == []


def test_transformer_code_falls_back_to_the_hierarchy(export_records):
    record = export_record(export_records, "J100000")
    assert meter_from_export(record | {"dtCode": ""}).transformer_code == "DT-001"
    record["hierarchy"]["dt"]["code"] = ""
    with pytest.raises(NormalizationError):
        meter_from_export(record | {"dtCode": ""})


def test_a_record_without_a_meter_id_is_rejected(export_records):
    with pytest.raises(NormalizationError):
        meter_from_export(export_record(export_records, "J100000") | {"meterId": " "})


@pytest.mark.parametrize(
    "geo",
    [{"lat": 0, "lng": 0}, {"lat": 95.0, "lng": 75.8}, {"lat": "", "lng": "75.8"}, {}, None],
    ids=["null-island", "out-of-range", "blank", "empty", "missing"],
)
def test_unusable_coordinates_are_dropped(export_records, geo):
    assert meter_from_export(export_record(export_records, "J100000") | {"geo": geo}).location is None


# ----------------------------------------------------------------------------- detail pages


def test_legacy_detail_page_matches_the_export(export_records, fixture_json):
    meter = meter_from_detail_page(decoded_page(fixture_json("detail_legacy_J100000.json")))
    exported = meter_from_export(export_record(export_records, "J100000"))
    for field in ("meter_id", "serial_number", "make", "phase", "status", "installation_type", "transformer_code"):
        assert getattr(meter, field) == getattr(exported, field), field
    assert meter.location is None  # detail pages carry no coordinates (a crawl keeps the last known ones)
    assert meter.reported_path == exported.reported_path


def test_v2_detail_page_nameplate_is_a_json_string(export_records, fixture_json):
    meter = meter_from_detail_page(decoded_page(fixture_json("detail_v2_J100004.json")))
    assert (meter.meter_id, meter.serial_number, meter.make) == ("J100004", "SE65293", "Genus")
    assert (meter.phase, meter.status, meter.installation_type) == (
        Phase.single,
        MeterStatus.faulty,
        InstallationType.ct_operated,
    )
    assert meter.location is None  # detail pages carry no coordinates
    assert meter.reported_path == meter_from_export(export_record(export_records, "J100004")).reported_path


def test_detail_page_drops_the_code_of_a_blank_name(fixture_json):
    meter = meter_from_detail_page(decoded_page(fixture_json("detail_blank_J100162.json")))
    assert meter.reported_path[NetworkLevel.feeder] == ReportedNode(code=None, name=None)  # export: F-003
    assert meter.reported_path[NetworkLevel.substation] == ReportedNode(code="SS-03", name="Substation 3")
    assert meter.transformer_code == "DT-003"


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Jaipur Zone 1 (Z-01)", ReportedNode(code="Z-01", name="Jaipur Zone 1")),
        ("Circle 6", ReportedNode(code=None, name="Circle 6")),  # blank code: the portal drops the brackets
        ("", ReportedNode(code=None, name=None)),  # blank name: the portal drops the code as well
        (None, ReportedNode(code=None, name=None)),
        ("Circle 6 ()", ReportedNode(code=None, name="Circle 6")),
        (" (C-01)", ReportedNode(code="C-01", name=None)),
        ("  Old Malviya Nagar Xfmr  (DT-007) ", ReportedNode(code="DT-007", name="Old Malviya Nagar Xfmr")),
    ],
)
def test_code_labels(label, expected):
    assert parse_code_label(label) == expected


# ----------------------------------------------------------------------------- scalars and readings


def test_timestamps_are_day_first_and_read_as_ist():
    ts = parse_portal_timestamp("01/06/2026 00:30")
    assert ts == datetime(2026, 6, 1, 0, 30, tzinfo=IST)
    assert ts.utcoffset() == timedelta(hours=5, minutes=30)
    assert ts.isoformat() == "2026-06-01T00:30:00+05:30"


@pytest.mark.parametrize("raw", ["2026-06-01 00:30", "01/06/2026", "", "01-06-2026 00:30", "01/06/2026 00:30:00"])
def test_unrecognised_timestamps(raw):
    with pytest.raises(NormalizationError):
        parse_portal_timestamp(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("48438.74", 48438.74),
        ("48024", 48024.0),
        (" 226 ", 226.0),
        ("1,234.5", 1234.5),
        (100, 100.0),
        ("", None),
        ("   ", None),
        (None, None),
        ("n/a", None),
        (True, None),
    ],
)
def test_numbers(raw, expected):
    assert parse_number(raw) == expected


def test_real_energy_rows(energy):
    readings = readings_from_portal(energy["J100089"])
    assert len(readings) == 31
    assert readings[0] == RawReading(datetime(2026, 6, 1, tzinfo=IST), 12510.64, 13511.5, 237.0)
    # the portal's blank duplicate of the last reading: '' → None, and kept (merging comes later)
    assert readings[-2:] == [
        RawReading(datetime(2026, 6, 30, tzinfo=IST), 12732.34, 13750.93, 229.0),
        RawReading(datetime(2026, 6, 30, tzinfo=IST), None, 13750.93, None),
    ]


def test_readings_are_sorted_and_bad_timestamps_dropped():
    rows = [
        {"timestamp": "02/06/2026 00:00", "kwh": "20.5", "kvah": "22.14", "voltR": "231"},
        {"timestamp": "2026-06-01 12:00", "kwh": "10", "kvah": "10.8", "voltR": "230"},
        {"timestamp": "01/06/2026 00:00", "kwh": "10", "kvah": "10.8", "voltR": "229"},
        {"kwh": "30"},
    ]
    assert [r.timestamp for r in readings_from_portal(rows)] == [
        datetime(2026, 6, 1, tzinfo=IST),
        datetime(2026, 6, 2, tzinfo=IST),
    ]


def test_impossible_dates_are_dropped_like_other_bad_timestamps():
    # Matches the DD/MM/YYYY HH:MM shape but is not a date: must be dropped, not crash the series.
    rows = [
        {"timestamp": "31/06/2026 00:00", "kwh": "10", "kvah": "10.8", "voltR": "230"},
        {"timestamp": "01/07/2026 00:00", "kwh": "20", "kvah": "21.6", "voltR": "230"},
    ]
    assert [r.timestamp for r in readings_from_portal(rows)] == [datetime(2026, 7, 1, tzinfo=IST)]


# ----------------------------------------------------------------------------- transformers


def test_transformer_rows(dt_rows):
    row = next(r for r in dt_rows if r["code"] == "DT-007")
    assert transformer_from_portal(row) == TransformerRecord(
        code="DT-007", name="Sanganer DT 7", feeder_code="F-007", capacity_kva=63.0
    )


def test_transformer_name_falls_back_to_the_code_and_a_code_is_required():
    assert transformer_from_portal({"code": "DT-099", "name": " ", "feederCode": "F-001"}).name == "DT-099"
    with pytest.raises(NormalizationError):
        transformer_from_portal({"name": "Nameless", "feederCode": "F-001"})
