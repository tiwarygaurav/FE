"""The devalue decoder, on hand-written payloads and on real `__data.json` pages."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from fake_portal import devalue_flatten

from urja_api.portal.devalue import DevalueError, unflatten


def test_values_are_indices_into_the_flat_array():
    assert unflatten([{"meterId": 1, "readings": 2}, "J100000", [3, 4], 226.5, True]) == {
        "meterId": "J100000",
        "readings": [226.5, True],
    }


def test_primitive_roots():
    assert unflatten(["J100000"]) == "J100000"
    assert unflatten([None]) is None


def test_shared_indices_decode_to_the_same_object():
    value = unflatten([{"first": 1, "second": 1, "status": 2}, {"status": 2}, "Faulty"])
    assert value["first"] is value["second"]
    assert value == {"first": {"status": "Faulty"}, "second": {"status": "Faulty"}, "status": "Faulty"}


def test_special_values():
    undefined, hole, nan, inf, negative_inf, negative_zero = unflatten([[-1, -2, -3, -4, -5, -6]])
    assert undefined is None
    assert hole is None
    assert math.isnan(nan)
    assert (inf, negative_inf) == (math.inf, -math.inf)
    assert negative_zero == 0
    assert math.copysign(1, negative_zero) == -1


def test_a_bare_special_is_a_whole_payload():
    assert unflatten(-1) is None
    assert unflatten(-4) == math.inf


def test_cycles_are_rebuilt_as_references():
    node = unflatten([{"name": 1, "self": 0, "children": 2}, "root", [0]])
    assert node["name"] == "root"
    assert node["self"] is node
    assert node["children"][0] is node


def test_tagged_types():
    value = unflatten(
        [
            {"date": 1, "set": 2, "map": 3, "big": 4, "boxed": 5, "bare": 6, "regexp": 7, "url": 8},
            ["Date", "2026-06-30T18:30:00.000Z"],
            ["Set", 9, 10],
            ["Map", 9, 10],
            ["BigInt", "90071992547409930"],
            ["Object", "boxed"],
            ["null", "key", 10],
            ["RegExp", "^J\\d+$", "i"],
            ["URL", "https://portal.test/meters"],
            "J100000",
            42,
        ]
    )
    assert value == {
        "date": datetime(2026, 6, 30, 18, 30, tzinfo=UTC),
        "set": ["J100000", 42],
        "map": {"J100000": 42},
        "big": 90071992547409930,
        "boxed": "boxed",
        "bare": {"key": 42},
        "regexp": "^J\\d+$",
        "url": "https://portal.test/meters",
    }


@pytest.mark.parametrize(
    "payload",
    [[], {"meterId": 1}, "J100000", None, 7, [{"meterId": 5}], [["Symbol", "x"]]],
    ids=["empty", "object", "string", "null", "non-special-int", "index-out-of-range", "unknown-tag"],
)
def test_invalid_payloads(payload):
    with pytest.raises(DevalueError):
        unflatten(payload)


@pytest.mark.parametrize(
    "payload",
    [[{"meterId": "1"}, "J100000"], [["Map", 1], "J100000"], [["Date", "30/06/2026"]]],
    ids=["non-integer-reference", "map-without-value", "unparseable-date"],
)
def test_malformed_payloads_raise_devalue_error(payload):
    # The portal client maps DevalueError (and only that) to PortalProtocolError, so any
    # other exception type escapes as an unhandled error.
    with pytest.raises(DevalueError):
        unflatten(payload)


def test_real_legacy_detail_page(fixture_json):
    page = fixture_json("detail_legacy_J100000.json")
    assert unflatten(page["nodes"][1]["data"]) == {"user": {"name": "Ops Desk", "email": "operator@urja.local"}}
    data = unflatten(page["nodes"][-1]["data"])
    assert data["meterId"] == "J100000"
    assert data["detail"]["data"][:2] == [
        {"parameterName": "Meter ID", "parameterValue": "J100000"},
        {"parameterName": "Serial No", "parameterValue": "SE33962"},
    ]
    assert data["hierarchy"]["Meter ID"] == "J100000"  # one string, referenced three times
    assert data["hierarchy"]["DT"] == "Malviya Nagar DT 1 (DT-001)"


def test_real_v2_detail_page_keeps_the_nameplate_as_a_json_string(fixture_json):
    data = unflatten(fixture_json("detail_v2_J100004.json")["nodes"][-1]["data"])
    assert data["detail"]["classData"].startswith('{"installed_meter":{"MeterId":"J100004"')
    assert data["hierarchy"]["Feeder"] == "Feeder 5 (F-005)"


def test_round_trip_with_the_fake_portal_encoder():
    value = {"meterId": "J100000", "tags": ["a", "a", 1.5, None, False], "nested": {"meterId": "J100000"}}
    assert unflatten(devalue_flatten(value)) == value


DEEP = [*([i + 1] for i in range(5000)), 0]  # 5,000 arrays nested inside each other


@pytest.mark.parametrize("payload", [[["Date", 5]], DEEP], ids=["date-tag-with-a-number", "nesting-too-deep"])
def test_payloads_that_break_the_decoder_in_other_ways_are_devalue_errors_too(payload):
    with pytest.raises(DevalueError):
        unflatten(payload)
