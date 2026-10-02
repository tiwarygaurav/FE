"""The SQLite index on its own: schema versioning, sorting and filtering over the fixture snapshot."""

from __future__ import annotations

import contextlib
import math
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from urja_api.domain.network import reconcile
from urja_api.domain.normalize import readings_from_portal
from urja_api.domain.readings import detect_interval_minutes, merge_duplicates
from urja_api.store import (
    EARTH_RADIUS_KM,
    SCHEMA_VERSION,
    SORT_COLUMNS,
    SYNC_RUNS_KEPT,
    MeterFilter,
    Store,
    haversine_km,
)

SYNCED_AT = datetime(2026, 7, 1, tzinfo=UTC)
J100000 = (26.938961, 75.830957)
JAIPUR = (26.92, 75.80)  # 50 km from here covers every fixture meter
# What each `sort=` key of GET /v1/meters orders by, written out rather than read from the
# store, so a key wired to the wrong column fails the sort tests.
SORT_KEY_COLUMNS = {
    "meter_id": "meter_id",
    "serial_number": "serial_number",
    "make": "make",
    "status": "status",
    "phase": "phase",
    "installation_type": "installation_type",
    "transformer": "transformer_code",
    "feeder": "feeder_code",
    "issues": "issue_count",
    "interval": "interval_minutes",
}

# A database written by an earlier version: readings_fetch had no interval column.
OLD_SCHEMA = """
CREATE TABLE readings (
    meter_id TEXT NOT NULL, ts TEXT NOT NULL, kwh REAL, kvah REAL, voltage REAL,
    duplicate INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (meter_id, ts)
) WITHOUT ROWID;
CREATE TABLE readings_fetch (
    meter_id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, reading_count INTEGER NOT NULL, first_ts TEXT, last_ts TEXT
);
CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
    source TEXT, meters INTEGER, transformers INTEGER, warnings TEXT NOT NULL DEFAULT '[]', error TEXT
);
INSERT INTO readings VALUES ('J100000', '2026-06-01T00:00:00+05:30', 12510.64, 13511.5, 237, 0);
INSERT INTO readings_fetch VALUES ('J100000', '2026-07-01T00:00:00+00:00', 1, NULL, NULL);
INSERT INTO sync_runs (started_at, status) VALUES ('2026-07-01T00:00:00+00:00', 'ok');
"""


@pytest.fixture
def store(meters, transformers) -> Iterator[Store]:
    """The fixture snapshot, stored the way a reference sync stores it."""
    store = Store(":memory:")
    network = reconcile(meters, transformers)
    store.replace_reference_data(
        meters=meters,
        paths=network.meter_paths,
        issues=network.meter_issues,
        transformers=transformers,
        transformer_paths=network.transformer_paths,
        name_variants={code: v for (_, code), v in network.name_variants.items()},
        synced_at=SYNCED_AT,
    )
    yield store
    store.close()


def cache_readings(store: Store, energy: dict[str, list[dict[str, str]]], *meter_ids: str) -> None:
    for meter_id in meter_ids:
        readings, duplicates = merge_duplicates(readings_from_portal(energy[meter_id]))
        store.replace_readings(
            meter_id, readings, duplicates, SYNCED_AT, detect_interval_minutes([r.timestamp for r in readings])
        )


def search(store: Store, **filters: Any) -> list[str]:
    rows, _ = store.search_meters(MeterFilter(**filters), limit=500, offset=0)
    return [row["meter_id"] for row, _ in rows]


# ----------------------------------------------------------------------------- schema versions


@pytest.mark.parametrize("version", [0, SCHEMA_VERSION + 1], ids=["older", "unknown"])
def test_a_database_from_another_schema_version_is_rebuilt(tmp_path, energy, version):
    path = tmp_path / "urja.sqlite3"
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executescript(OLD_SCHEMA)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()

    store = Store(path)
    try:
        # The cache is simply dropped (it only mirrors the portal) ...
        assert store.get_readings("J100000") == []
        assert store.readings_fetch_info("J100000") is None
        assert store.last_sync_runs() == []
        # ... and rebuilt with the current layout, so new writes work.
        cache_readings(store, energy, "J100000")
        assert store.readings_fetch_info("J100000")["interval_minutes"] == 30
    finally:
        store.close()

    with contextlib.closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_a_database_with_the_current_schema_is_kept(tmp_path):
    path = tmp_path / "urja.sqlite3"
    store = Store(path)
    run_id = store.start_sync_run(SYNCED_AT)
    store.close()

    store = Store(path)
    try:
        assert [row["id"] for row in store.last_sync_runs()] == [run_id]
    finally:
        store.close()


# ----------------------------------------------------------------------------- sorting


def expected_order(store: Store, column: str, descending: bool) -> list[str]:
    """Sorted by `column`, ties in ascending meter id order (Python's sort is stable, even reversed).

    NULLs (an interval not known yet) sort first ascending and last descending, as in SQLite.
    """
    rows, _ = store.search_meters(MeterFilter(), limit=10_000, offset=0)  # every meter, by meter_id
    by_id = [row for row, _ in rows]

    def key(row):
        return (row[column] is not None, row[column] if row[column] is not None else 0)

    return [row["meter_id"] for row in sorted(by_id, key=key, reverse=descending)]


@pytest.mark.parametrize("descending", [False, True], ids=["asc", "desc"])
@pytest.mark.parametrize(("key", "column"), [pytest.param(k, c, id=k) for k, c in SORT_KEY_COLUMNS.items()])
def test_sort_by_any_key_with_ties_broken_by_meter_id(store, key, column, descending):
    assert search(store, sort=f"-{key}" if descending else key) == expected_order(store, column, descending)


def test_every_sort_key_of_the_listing_is_covered():
    assert set(SORT_COLUMNS) == set(SORT_KEY_COLUMNS)


def test_near_sorts_by_another_key_and_still_reports_distances(store):
    rows, total = store.search_meters(MeterFilter(near=J100000, radius_km=6, sort="-meter_id"), limit=500, offset=0)
    distances = {row["meter_id"]: distance for row, distance in rows}
    assert list(distances) == ["J100218", "J100100", "J100011", "J100000"]
    assert total == 4
    assert distances["J100000"] == 0
    assert all(0 < distance <= 6 for meter_id, distance in distances.items() if meter_id != "J100000")
    assert search(store, near=J100000, radius_km=6, sort="make") == ["J100000", "J100011", "J100100", "J100218"]


def test_near_breaks_ties_in_a_descending_sort_like_the_listing_does(store):
    # A radius covering every meter must not change the order of a sort by another key.
    everywhere = {"near": JAIPUR, "radius_km": 50}
    assert search(store, sort="make", **everywhere) == search(store, sort="make")
    assert search(store, sort="-make", **everywhere) == search(store, sort="-make")


# ----------------------------------------------------------------------------- filters


def test_interval_filter_matches_only_meters_with_cached_readings(store, energy):
    assert search(store, interval_minutes=30) == []
    cache_readings(store, energy, "J100000", "J100001", "J100040")
    assert search(store, interval_minutes=30) == ["J100000", "J100001"]
    assert search(store, interval_minutes=1440) == ["J100040"]
    assert search(store, interval_minutes=60) == []
    assert search(store, interval_minutes=30, near=J100000, radius_km=6) == ["J100000"]
    assert (store.get_meter("J100040")["interval_minutes"], store.get_meter("J100089")["interval_minutes"]) == (
        1440,
        None,
    )


def test_near_and_bbox_together_match_only_meters_inside_both(store):
    around_j100000 = (26.93, 75.82, 26.94, 75.84)  # min_lat, min_lng, max_lat, max_lng
    assert search(store, bbox=around_j100000) == ["J100000", "J100218"]
    assert sorted(search(store, bbox=around_j100000, near=JAIPUR, radius_km=50)) == ["J100000", "J100218"]


@pytest.mark.parametrize("bearing", ["north", "east"])
def test_near_includes_a_meter_just_inside_the_radius(store, bearing):
    lat, lng = J100000
    distance = 1.999
    if bearing == "north":  # search from due south of the meter
        point = (lat - math.degrees(distance / EARTH_RADIUS_KM), lng)
    else:  # due west, on the same parallel
        half = math.asin(math.sin(distance / (2 * EARTH_RADIUS_KM)) / math.cos(math.radians(lat)))
        point = (lat, lng - math.degrees(2 * half))
    assert haversine_km(*point, lat, lng) == pytest.approx(distance)
    assert "J100000" in search(store, near=point, radius_km=2)


def test_near_excludes_a_meter_a_hair_outside_the_radius(store):
    # 2.00004 km away: rounding the distance to 4 decimals (2.0000) before comparing it with
    # the radius would wrongly let this meter in.
    lat, lng = J100000
    point = (lat - math.degrees(2.00004 / EARTH_RADIUS_KM), lng)
    assert haversine_km(*point, lat, lng) == pytest.approx(2.00004)
    assert "J100000" not in search(store, near=point, radius_km=2)
    assert "J100000" in search(store, near=point, radius_km=2.0001)


def test_pruning_sync_runs_never_drops_the_last_successful_one(tmp_path):
    store = Store(tmp_path / "runs.sqlite3")
    at = datetime(2026, 10, 1, tzinfo=UTC)
    ok = store.start_sync_run(at)
    store.finish_sync_run(ok, at, "ok", source="export", meters=20, transformers=11)
    for _ in range(SYNC_RUNS_KEPT + 5):  # a long outage
        store.finish_sync_run(store.start_sync_run(at), at, "failed", error="PortalUnavailable: down")
    assert store.last_successful_sync()["id"] == ok  # still says how old the snapshot is
    assert len(store.last_sync_runs(1_000)) == SYNC_RUNS_KEPT + 1
    store.close()
