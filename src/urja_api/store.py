"""SQLite-backed local index.

Why a local copy at all? The portal can only look meters up by id/serial, 20 at a time,
and every `/portal/*` call draws on one rate budget. Keeping a synced copy lets this API answer
questions the portal can't (filter by any attribute or network node, "what's near this
point?", fleet-wide consumption) without putting load on it, and keep serving
last-known-good data when the portal is down.

SQLite fits the scale (hundreds to low millions of rows) and ships with Python. The
R*Tree module gives a real spatial index. Reference data is replaced in a single
transaction per sync, so readers never see a half-written snapshot.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .domain.models import DataIssue, NetworkPath
from .domain.normalize import MeterRecord, RawReading, TransformerRecord

# Bump when the schema changes. The database only caches portal data, so an old file is
# simply dropped and rebuilt on the next sync rather than migrated.
SCHEMA_VERSION = 4
_TABLES = ("meters", "meter_geo", "transformers", "readings", "readings_fetch", "sync_runs")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meters (
    meter_id          TEXT PRIMARY KEY,
    serial_number     TEXT NOT NULL,
    make              TEXT NOT NULL,
    phase             TEXT NOT NULL,
    status            TEXT NOT NULL,
    installation_type TEXT NOT NULL,
    transformer_code  TEXT NOT NULL,
    latitude          REAL,
    longitude         REAL,
    zone_code         TEXT,
    circle_code       TEXT,
    division_code     TEXT,
    subdivision_code  TEXT,
    substation_code   TEXT,
    feeder_code       TEXT,
    network_json      TEXT NOT NULL,
    issues_json       TEXT NOT NULL,
    issue_count       INTEGER NOT NULL,
    synced_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS meters_status ON meters(status);
CREATE INDEX IF NOT EXISTS meters_make ON meters(make);
CREATE INDEX IF NOT EXISTS meters_transformer ON meters(transformer_code);
CREATE INDEX IF NOT EXISTS meters_feeder ON meters(feeder_code);
CREATE INDEX IF NOT EXISTS meters_substation ON meters(substation_code);
CREATE INDEX IF NOT EXISTS meters_subdivision ON meters(subdivision_code);
CREATE INDEX IF NOT EXISTS meters_division ON meters(division_code);
CREATE INDEX IF NOT EXISTS meters_circle ON meters(circle_code);
CREATE INDEX IF NOT EXISTS meters_zone ON meters(zone_code);

-- Spatial index: one point-sized box per meter, keyed by meters.rowid.
CREATE VIRTUAL TABLE IF NOT EXISTS meter_geo USING rtree(id, min_lat, max_lat, min_lng, max_lng);

CREATE TABLE IF NOT EXISTS transformers (
    code               TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    feeder_code        TEXT NOT NULL,
    capacity_kva       REAL,
    network_json       TEXT NOT NULL,
    name_variants_json TEXT NOT NULL,
    synced_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
    meter_id  TEXT NOT NULL,
    ts        TEXT NOT NULL,   -- ISO 8601 with +05:30; one offset, so text order == time order
    kwh       REAL,
    kvah      REAL,
    voltage   REAL,
    duplicate INTEGER NOT NULL DEFAULT 0,  -- 1: portal sent this timestamp twice, 2: with conflicting values
    PRIMARY KEY (meter_id, ts)
) WITHOUT ROWID;

-- Cache bookkeeping: when each meter's series was last fetched from the portal.
CREATE TABLE IF NOT EXISTS readings_fetch (
    meter_id         TEXT PRIMARY KEY,
    fetched_at       TEXT NOT NULL,
    reading_count    INTEGER NOT NULL,
    first_ts         TEXT,
    last_ts          TEXT,
    interval_minutes INTEGER,
    content_hash     TEXT      -- hash of the portal's payload, to skip rewriting unchanged series
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,          -- running | ok | failed
    source       TEXT,                   -- export | crawl
    meters       INTEGER,
    transformers INTEGER,
    warnings     TEXT NOT NULL DEFAULT '[]',
    error        TEXT
);
"""

LEVEL_COLUMNS = {
    "zone": "zone_code",
    "circle": "circle_code",
    "division": "division_code",
    "subdivision": "subdivision_code",
    "substation": "substation_code",
    "feeder": "feeder_code",
    "transformer": "transformer_code",
}
EARTH_RADIUS_KM = 6371.0088
SYNC_RUNS_KEPT = 100
# Sortable fields of GET /v1/meters -> SQL column ("distance" is handled separately).
SORT_COLUMNS = {
    "meter_id": "m.meter_id",
    "serial_number": "m.serial_number",
    "make": "m.make",
    "status": "m.status",
    "phase": "m.phase",
    "installation_type": "m.installation_type",
    "transformer": "m.transformer_code",
    "feeder": "m.feeder_code",
    "issues": "m.issue_count",
    "interval": "f.interval_minutes",
}
# The response's field names work as sort keys too.
SORT_ALIASES = {
    "transformer_code": "transformer",
    "feeder_code": "feeder",
    "data_issue_count": "issues",
    "reading_interval_minutes": "interval",
    "distance_km": "distance",
}


def _nulls_first(value: Any) -> tuple[bool, Any]:
    return (value is not None, value if value is not None else 0)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class MeterFilter:
    q: str | None = None
    status: Sequence[str] = ()
    make: Sequence[str] = ()
    phase: str | None = None
    installation_type: str | None = None
    network: dict[str, str] | None = None  # level -> code
    has_issues: bool | None = None
    interval_minutes: int | None = None
    near: tuple[float, float] | None = None
    radius_km: float = 1.0
    bbox: tuple[float, float, float, float] | None = None  # min_lat, min_lng, max_lat, max_lng
    sort: str | None = None  # a SORT_COLUMNS/SORT_ALIASES key or "distance", optionally prefixed with "-"


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Bumped on every write, so derived views can be memoised safely.
        self.readings_version = 0
        self.reference_version = 0
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            if self._conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                for table in _TABLES:
                    self._conn.execute(f"DROP TABLE IF EXISTS {table}")
                self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:  # let a write in a worker thread finish its transaction first
            self._conn.close()

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------ reference data

    def replace_reference_data(
        self,
        meters: list[MeterRecord],
        paths: dict[str, NetworkPath],
        issues: dict[str, list[DataIssue]],
        transformers: list[TransformerRecord],
        transformer_paths: dict[str, NetworkPath],
        name_variants: dict[str, list[str]],
        synced_at: datetime,
    ) -> None:
        stamp = synced_at.isoformat()
        with self._lock:
            conn = self._conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM meters")
                conn.execute("DELETE FROM meter_geo")
                conn.execute("DELETE FROM transformers")
                for m in meters:
                    path = paths[m.meter_id]
                    meter_issues = issues.get(m.meter_id, [])
                    cur = conn.execute(
                        """INSERT INTO meters VALUES
                           (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            m.meter_id,
                            m.serial_number,
                            m.make,
                            m.phase.value,
                            m.status.value,
                            m.installation_type.value,
                            m.transformer_code,
                            m.location.latitude if m.location else None,
                            m.location.longitude if m.location else None,
                            path.zone.code,
                            path.circle.code,
                            path.division.code,
                            path.subdivision.code,
                            path.substation.code,
                            path.feeder.code,
                            path.model_dump_json(),
                            json.dumps([i.model_dump(mode="json") for i in meter_issues]),
                            len(meter_issues),
                            stamp,
                        ),
                    )
                    if m.location:
                        lat, lng = m.location.latitude, m.location.longitude
                        conn.execute("INSERT INTO meter_geo VALUES (?,?,?,?,?)", (cur.lastrowid, lat, lat, lng, lng))
                known = {t.code: t for t in transformers}
                for code, path in transformer_paths.items():
                    t = known.get(code)
                    conn.execute(
                        "INSERT INTO transformers VALUES (?,?,?,?,?,?,?)",
                        (
                            code,
                            t.name if t else (path.transformer.name or code),
                            t.feeder_code if t else path.feeder.code,
                            t.capacity_kva if t else None,
                            path.model_dump_json(),
                            json.dumps(name_variants.get(code, [])),
                            stamp,
                        ),
                    )
                conn.execute("COMMIT")
                self.reference_version += 1
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def touch_reference_data(self, synced_at: datetime) -> None:
        """Record that a sync found the snapshot unchanged: restamp it without rewriting it
        (and without bumping `reference_version`, so memoised views stay valid)."""
        stamp = synced_at.isoformat()
        with self._lock:
            self._conn.execute("UPDATE meters SET synced_at = ?", (stamp,))
            self._conn.execute("UPDATE transformers SET synced_at = ?", (stamp,))

    def has_reference_data(self) -> bool:
        return bool(self._query("SELECT 1 FROM meters LIMIT 1"))

    def snapshot_counts(self) -> sqlite3.Row:
        """What the current snapshot holds, for judging whether a new one lost too much."""
        return self._query(
            """SELECT (SELECT COUNT(*) FROM meters) AS meters,
                      (SELECT COUNT(*) FROM meters WHERE latitude IS NOT NULL) AS located,
                      (SELECT COUNT(*) FROM transformers) AS transformers,
                      (SELECT COUNT(*) FROM transformers WHERE capacity_kva IS NOT NULL) AS rated"""
        )[0]

    # ------------------------------------------------------------------ meters

    _METERS = """SELECT m.*, f.interval_minutes, f.fetched_at AS readings_fetched_at, f.reading_count,
               f.first_ts, f.last_ts
        FROM meters m LEFT JOIN readings_fetch f ON f.meter_id = m.meter_id"""

    def get_meter(self, meter_id: str) -> sqlite3.Row | None:
        rows = self._query(f"{self._METERS} WHERE m.meter_id = ?", (meter_id,))
        return rows[0] if rows else None

    def all_meters(self) -> dict[str, sqlite3.Row]:
        return {r["meter_id"]: r for r in self._query("SELECT * FROM meters ORDER BY meter_id")}

    def meter_ids(self) -> list[str]:
        return [r["meter_id"] for r in self._query("SELECT meter_id FROM meters ORDER BY meter_id")]

    def search_meters(
        self, f: MeterFilter, limit: int, offset: int
    ) -> tuple[list[tuple[sqlite3.Row, float | None]], int]:
        where, params = ["1=1"], []
        if f.q:
            # Escape LIKE wildcards: '%' and '_' in a query are literal characters here
            # (the portal's own search passes '%' through as a wildcard, and so presumably '_').
            needle = f.q.strip().lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(lower(m.meter_id) LIKE ? ESCAPE '\\' OR lower(m.serial_number) LIKE ? ESCAPE '\\')")
            params += [f"%{needle}%", f"%{needle}%"]
        for column, values in (("status", f.status), ("make", f.make)):
            if values:
                where.append(f"lower(m.{column}) IN ({','.join('?' * len(values))})")
                params += [v.lower() for v in values]
        if f.phase:
            where.append("m.phase = ?")
            params.append(f.phase)
        if f.installation_type:
            where.append("m.installation_type = ?")
            params.append(f.installation_type)
        for level, code in (f.network or {}).items():
            where.append(f"m.{LEVEL_COLUMNS[level]} = ?")
            params.append(code)
        if f.has_issues is not None:
            where.append("m.issue_count > 0" if f.has_issues else "m.issue_count = 0")
        if f.interval_minutes is not None:
            where.append("f.interval_minutes = ?")
            params.append(f.interval_minutes)

        box = f.bbox
        if f.near:
            lat, lng = f.near
            # Same sphere as haversine_km, plus 1 % so meters right at the radius are never
            # cut by the prefilter (the exact distance check below decides).
            dlat = math.degrees(f.radius_km / EARTH_RADIUS_KM) * 1.01
            dlng = dlat / max(0.01, math.cos(math.radians(lat)))
            near_box = (lat - dlat, lng - dlng, lat + dlat, lng + dlng)
            # With both near= and bbox=, a meter must be inside both.
            box = near_box if box is None else (*map(max, box[:2], near_box[:2]), *map(min, box[2:], near_box[2:]))
        source = self._METERS
        if box:
            source += " JOIN meter_geo g ON g.id = m.rowid"
            where.append("g.max_lat >= ? AND g.min_lat <= ? AND g.max_lng >= ? AND g.min_lng <= ?")
            params += [box[0], box[2], box[1], box[3]]

        sql_where = " AND ".join(where)
        descending = (f.sort or "").startswith("-")
        key = (f.sort or "").lstrip("-") or ("distance" if f.near else "meter_id")
        key = SORT_ALIASES.get(key, key)
        if f.near:
            # R*Tree gives the bounding-box candidates; exact distance is computed here.
            candidates = self._query(f"{source} WHERE {sql_where}", params)
            lat, lng = f.near
            # Filter and rank on the exact distance; round only what is returned, or a meter
            # a hair outside the radius would round its way in.
            scored = [(row, haversine_km(lat, lng, row["latitude"], row["longitude"])) for row in candidates]
            within = [(row, d) for row, d in scored if d <= f.radius_km]
            # Two stable passes, so ties stay in ascending meter_id even for a descending
            # sort, exactly like the SQL path below.
            within.sort(key=lambda pair: pair[0]["meter_id"])
            if key == "distance":
                within.sort(key=lambda pair: pair[1], reverse=descending)
            else:
                column = SORT_COLUMNS[key].split(".", 1)[1]
                # NULLs first when ascending, last when descending: SQLite's order.
                within.sort(key=lambda pair: _nulls_first(pair[0][column]), reverse=descending)
            return [(row, round(d, 4)) for row, d in within[offset : offset + limit]], len(within)

        total = self._query(f"SELECT COUNT(*) FROM ({source} WHERE {sql_where})", params)[0][0]
        order = f"{SORT_COLUMNS[key]} {'DESC' if descending else 'ASC'}, m.meter_id"
        rows = self._query(f"{source} WHERE {sql_where} ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset])
        return [(row, None) for row in rows], total

    def meter_status_by_transformer(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for r in self._query("SELECT transformer_code, status, COUNT(*) AS n FROM meters GROUP BY 1, 2"):
            result.setdefault(r["transformer_code"], {})[r["status"]] = r["n"]
        return result

    def issues(self) -> list[tuple[str, list[dict[str, Any]]]]:
        rows = self._query("SELECT meter_id, issues_json FROM meters WHERE issue_count > 0 ORDER BY meter_id")
        return [(r["meter_id"], json.loads(r["issues_json"])) for r in rows]

    # ------------------------------------------------------------------ transformers

    _TRANSFORMERS = """SELECT t.*, COUNT(m.meter_id) AS meter_count
        FROM transformers t LEFT JOIN meters m ON m.transformer_code = t.code"""

    def list_transformers(self) -> list[sqlite3.Row]:
        return self._query(f"{self._TRANSFORMERS} GROUP BY t.code ORDER BY t.code")

    def get_transformer(self, code: str) -> sqlite3.Row | None:
        rows = self._query(f"{self._TRANSFORMERS} WHERE t.code = ? GROUP BY t.code", (code,))
        return rows[0] if rows else None

    def transformer_paths(self) -> dict[str, NetworkPath]:
        return {
            r["code"]: NetworkPath.model_validate_json(r["network_json"])
            for r in self._query("SELECT code, network_json FROM transformers")
        }

    # ------------------------------------------------------------------ readings

    def replace_readings(
        self,
        meter_id: str,
        readings: list[RawReading],
        duplicates: dict[datetime, int],
        fetched_at: datetime,
        interval_minutes: int | None,
        content_hash: str | None = None,
    ) -> bool:
        """Store a meter's series. Returns False (and only refreshes `fetched_at`) when
        `content_hash` matches what is already stored: the portal has no ETags, so change
        detection happens here, and unchanged data doesn't invalidate derived views."""
        with self._lock:
            conn = self._conn
            if content_hash is not None:
                stored = conn.execute(
                    "SELECT content_hash FROM readings_fetch WHERE meter_id = ?", (meter_id,)
                ).fetchone()
                if stored is not None and stored["content_hash"] == content_hash:
                    conn.execute(
                        "UPDATE readings_fetch SET fetched_at = ? WHERE meter_id = ?",
                        (fetched_at.isoformat(), meter_id),
                    )
                    return False
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("DELETE FROM readings WHERE meter_id = ?", (meter_id,))
                conn.executemany(
                    "INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?)",
                    [
                        (
                            meter_id,
                            r.timestamp.isoformat(),
                            r.energy_kwh,
                            r.energy_kvah,
                            r.voltage_v,
                            duplicates.get(r.timestamp, 0),
                        )
                        for r in readings
                    ],
                )
                self.readings_version += 1
                conn.execute(
                    "INSERT OR REPLACE INTO readings_fetch VALUES (?,?,?,?,?,?,?)",
                    (
                        meter_id,
                        fetched_at.isoformat(),
                        len(readings),
                        readings[0].timestamp.isoformat() if readings else None,
                        readings[-1].timestamp.isoformat() if readings else None,
                        interval_minutes,
                        content_hash,
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            return True

    def readings_fetch_info(self, meter_id: str) -> sqlite3.Row | None:
        """A meter's cache bookkeeping, plus whether the cached series holds any kWh value."""
        rows = self._query(
            "SELECT f.*, EXISTS (SELECT 1 FROM readings r WHERE r.meter_id = f.meter_id AND r.kwh IS NOT NULL) "
            "AS has_kwh FROM readings_fetch f WHERE f.meter_id = ?",
            (meter_id,),
        )
        return rows[0] if rows else None

    def readings_fetch_summary(self) -> sqlite3.Row:
        return self._query(
            "SELECT COUNT(*) AS meters, MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest, "
            "MIN(first_ts) AS first_ts, MAX(last_ts) AS last_ts FROM readings_fetch"
        )[0]

    def get_readings(self, meter_id: str, start: str | None = None, end: str | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM readings WHERE meter_id = ?", [meter_id]
        if start:
            sql += " AND ts >= ?"
            params.append(start)
        if end:
            sql += " AND ts <= ?"
            params.append(end)
        return self._query(sql + " ORDER BY ts", params)

    def readings_for_all(self) -> dict[str, list[sqlite3.Row]]:
        result: dict[str, list[sqlite3.Row]] = {}
        for row in self._query("SELECT * FROM readings ORDER BY meter_id, ts"):
            result.setdefault(row["meter_id"], []).append(row)
        return result

    # ------------------------------------------------------------------ sync runs

    def start_sync_run(self, started_at: datetime) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')", (started_at.isoformat(),)
            )
            return int(cur.lastrowid)

    def finish_sync_run(
        self,
        run_id: int,
        finished_at: datetime,
        status: str,
        *,
        source: str | None = None,
        meters: int | None = None,
        transformers: int | None = None,
        warnings: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE sync_runs SET finished_at=?, status=?, source=?, meters=?, transformers=?,
                   warnings=?, error=? WHERE id=?""",
                (
                    finished_at.isoformat(),
                    status,
                    source,
                    meters,
                    transformers,
                    json.dumps(warnings or []),
                    error,
                    run_id,
                ),
            )
            # Keep the history bounded: the last SYNC_RUNS_KEPT runs are plenty for /v1/status.
            # The latest successful run always stays: it is what says how old the snapshot is.
            self._conn.execute(
                """DELETE FROM sync_runs WHERE id <= ? - ?
                   AND id <> (SELECT COALESCE(MAX(id), -1) FROM sync_runs WHERE status = 'ok')""",
                (run_id, SYNC_RUNS_KEPT),
            )

    def last_sync_runs(self, limit: int = 5) -> list[sqlite3.Row]:
        return self._query("SELECT * FROM sync_runs ORDER BY id DESC LIMIT ?", (limit,))

    def last_successful_sync(self) -> sqlite3.Row | None:
        rows = self._query("SELECT * FROM sync_runs WHERE status = 'ok' ORDER BY id DESC LIMIT 1")
        return rows[0] if rows else None
