"""Benchmark the local index's proximity search at larger fleet sizes.

Backs the README's note on the data scale at which the SQLite index would struggle. For
each size, synthetic meters are spread uniformly over the bounding box of the real data,
on a synthetic network that grows with them, and written into a throwaway on-disk
`Store` with `replace_reference_data` (the sync's write path): once into the empty store,
then again into the populated one, as every later sync does. Queries go through
`Store.search_meters(MeterFilter(near=...))`, the code behind `GET /v1/meters?near=`: an
R*Tree bounding-box prefilter in SQLite, then exact haversine distances and ordering in
Python. The baseline is a naive scan over every meter held in a Python list: no index, no
SQLite.

    uv run python scripts/bench_geo.py                  # 403, 10k, 100k and 1M meters
    uv run python scripts/bench_geo.py --max 100000     # drop the sizes above 100k

The markdown report goes to stdout, progress to stderr.
"""

from __future__ import annotations

import argparse
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache, partial
from pathlib import Path

from urja_api.domain.models import InstallationType, Location, MeterStatus, NetworkNodeRef, NetworkPath, Phase
from urja_api.domain.normalize import MeterRecord, TransformerRecord
from urja_api.store import MeterFilter, Store, haversine_km

Point = tuple[float, float]

# Bounding box of the portal's 403 meters.
LAT_RANGE = (26.7875, 27.0375)
LNG_RANGE = (75.6619, 75.9128)
CENTRE: Point = (sum(LAT_RANGE) / 2, sum(LNG_RANGE) / 2)
PAGE_SIZE = 50  # the API's default `limit`; search_meters ranks every candidate either way

# Synthetic network: DTs grow with the fleet (10 meters each, as on the portal) and every level
# above has a tenth as many nodes as the one below it.
METERS_PER_DT = 10
FAN_OUT = 10
LEVELS_ABOVE_DT = (
    ("feeder", "F"),
    ("substation", "SS"),
    ("subdivision", "SD"),
    ("division", "D"),
    ("circle", "C"),
    ("zone", "Z"),
)
MAKES = ("HPL", "Genus", "Secure", "Allied", "L&T")
STATUSES = (MeterStatus.installed, MeterStatus.faulty, MeterStatus.decommissioned)


@dataclass
class SizeResult:
    size: int
    build_s: float
    resync_s: float
    db_mb: float
    plan: str
    latency_s: dict[float, list[float]] = field(default_factory=dict)  # per radius, one entry per query
    hits: dict[float, list[int]] = field(default_factory=dict)
    naive_s: list[float] = field(default_factory=list)
    naive_hits: dict[float, list[int]] = field(default_factory=dict)


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def synthetic_network(meter_count: int) -> dict[str, NetworkPath]:
    """Paths by DT code."""

    @cache
    def node(prefix: str, index: int) -> NetworkNodeRef:
        return NetworkNodeRef(code=f"{prefix}-{index:06d}", name=f"{prefix} {index}")

    return {
        f"DT-{k:06d}": NetworkPath(
            transformer=node("DT", k),
            **{level: node(prefix, k // FAN_OUT**depth) for depth, (level, prefix) in enumerate(LEVELS_ABOVE_DT, 1)},
        )
        for k in range(max(1, meter_count // METERS_PER_DT))
    }


def synthetic_meters(count: int, dt_codes: Sequence[str], rng: random.Random) -> list[MeterRecord]:
    """Valid records in the portal's vocabulary. Every indexed column varies, as in a real fleet, so
    the build pays for inserts spread across each index rather than appends to a single key."""
    # Locations first, so they don't depend on the attributes drawn after them.
    locations = [Location(latitude=rng.uniform(*LAT_RANGE), longitude=rng.uniform(*LNG_RANGE)) for _ in range(count)]
    return [
        MeterRecord(
            meter_id=f"J{1_000_000 + i}",
            serial_number=f"SN{i:07d}",
            make=rng.choice(MAKES),
            phase=rng.choice((Phase.single, Phase.three)),
            status=rng.choice(STATUSES),
            installation_type=rng.choice((InstallationType.whole_current, InstallationType.ct_operated)),
            transformer_code=rng.choice(dt_codes),
            location=location,
            reported_path={},
            source="synthetic",
        )
        for i, location in enumerate(locations)
    ]


def build(store: Store, meters: list[MeterRecord], network: dict[str, NetworkPath]) -> float:
    paths = {m.meter_id: network[m.transformer_code] for m in meters}
    transformers = [
        TransformerRecord(code, path.transformer.name or code, path.feeder.code, capacity_kva=100.0)
        for code, path in network.items()
    ]
    started = time.perf_counter()
    store.replace_reference_data(
        meters=meters,
        paths=paths,
        issues={},
        transformers=transformers,
        transformer_paths=network,
        name_variants={},
        synced_at=datetime.now(UTC),
    )
    return time.perf_counter() - started


def store_hits(store: Store, radius_km: float, point: Point) -> int:
    _, total = store.search_meters(MeterFilter(near=point, radius_km=radius_km), PAGE_SIZE, 0)
    return total


def naive_hits(rows: list[tuple[float, float, str]], radii: Sequence[float], point: Point) -> list[int]:
    """No index: the distance to every meter, then the store's filter and ordering for the largest radius."""
    lat, lng = point
    radius = max(radii)
    within = sorted(
        (d, meter_id) for m_lat, m_lng, meter_id in rows if (d := haversine_km(lat, lng, m_lat, m_lng)) <= radius
    )
    return [sum(d <= r for d, _ in within) for r in radii]


def timed[T](run: Callable[[Point], T], points: Sequence[Point], budget_s: float) -> tuple[list[float], list[T]]:
    """Time `run` at each point, stopping early once the next call would push the series past `budget_s`."""
    seconds: list[float] = []
    results: list[T] = []
    for point in points:
        if seconds and sum(seconds) / len(seconds) * (len(seconds) + 1) > budget_s:
            break
        started = time.perf_counter()
        results.append(run(point))
        seconds.append(time.perf_counter() - started)
    return seconds, results


def db_megabytes(store: Store) -> float:
    # Logical size: right after the build, pages may still sit in the WAL rather than the main file.
    conn = store._conn
    return conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0] / 1e6


def query_plan(store: Store, radius_km: float) -> str:
    """The plan SQLite picks for the statement `search_meters` actually runs."""
    conn = store._conn
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        store_hits(store, radius_km, CENTRE)
    finally:
        conn.set_trace_callback(None)
    sql = next(s for s in statements if "meter_geo" in s)
    return "; ".join(row["detail"] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}"))


def bench_size(size: int, points: list[Point], radii: Sequence[float], budget_s: float, seed: int) -> SizeResult:
    started = time.perf_counter()
    network = synthetic_network(size)
    meters = synthetic_meters(size, list(network), random.Random(seed + size))
    rows = [(m.location.latitude, m.location.longitude, m.meter_id) for m in meters if m.location]
    log(f"{size:>9,} meters: records generated in {time.perf_counter() - started:.1f} s")
    with tempfile.TemporaryDirectory(prefix="urja-bench-", ignore_cleanup_errors=True) as tmp:
        store = Store(Path(tmp) / "bench.sqlite3")
        try:
            build_s = build(store, meters, network)
            resync_s = build(store, meters, network)
            del meters, network
            result = SizeResult(size, build_s, resync_s, db_megabytes(store), query_plan(store, max(radii)))
            log(f"{'':>9}  built in {build_s:.1f} s, re-synced in {resync_s:.1f} s ({result.db_mb:,.1f} MB)")
            for radius in radii:
                run = partial(store_hits, store, radius)
                run(CENTRE)  # warm-up, not counted
                result.latency_s[radius], result.hits[radius] = timed(run, points, budget_s)
                log(f"{'':>9}  near {radius:g} km: p50 {statistics.median(result.latency_s[radius]) * 1e3:.2f} ms")
        finally:
            store.close()
    result.naive_s, per_point = timed(partial(naive_hits, rows, radii), points, budget_s)
    result.naive_hits = {radius: [hits[i] for hits in per_point] for i, radius in enumerate(radii)}
    log(f"{'':>9}  naive: p50 {statistics.median(result.naive_s) * 1e3:.1f} ms over {len(result.naive_s)} queries")
    return result


def ms(seconds: float) -> str:
    value = seconds * 1e3
    return f"{value:.2f}" if value < 10 else f"{value:.1f}" if value < 100 else f"{value:,.0f}"


def sampled(text: str, runs: int, points: int) -> str:
    return text if runs == points else f"{text} (n={runs})"


def p50_p95(seconds: list[float]) -> str:
    if len(seconds) < 2:
        return f"{ms(seconds[0])} / -"
    cuts = statistics.quantiles(seconds, n=20, method="inclusive")
    return f"{ms(cuts[9])} / {ms(cuts[18])}"


def consistency(result: SizeResult, radii: Sequence[float]) -> str:
    """Store totals against the naive scan, for the queries both ran."""
    pairs = [
        (stored, naive)
        for radius in radii
        for stored, naive in zip(result.hits[radius], result.naive_hits[radius], strict=False)
    ]
    diffs = Counter(stored - naive for stored, naive in pairs if stored != naive)
    text = f"{result.size:,}: {len(pairs) - diffs.total()}/{len(pairs)} agree"
    if diffs:
        text += " (store - naive: " + ", ".join(f"{d:+d} x{n}" for d, n in sorted(diffs.items())) + ")"
    return text


def report(results: list[SizeResult], radii: Sequence[float], points: int, budget_s: float) -> str:
    box_km = haversine_km(LAT_RANGE[0], CENTRE[1], LAT_RANGE[1], CENTRE[1]) * haversine_km(
        CENTRE[0], LNG_RANGE[0], CENTRE[0], LNG_RANGE[1]
    )
    radii_label = " / ".join(f"{r:g}" for r in radii)
    head = [
        "size",
        "build s",
        "re-sync s",
        *(f"near {r:g} km p50/p95 ms" for r in radii),
        f"avg hits ({radii_label} km)",
        f"naive scan p50 ms ({max(radii):g} km)",
    ]
    lines = [
        f"Python {platform.python_version()} ({platform.python_implementation()}), SQLite {sqlite3.sqlite_version}, "
        f"{platform.platform()}, {platform.processor() or platform.machine()}",
        "",
        "| " + " | ".join(head) + " |",
        "|" + "---:|" * len(head),
    ]
    for res in results:
        cells = [
            f"{res.size:,}",
            f"{res.build_s:.2f}",
            f"{res.resync_s:.2f}",
            *(sampled(p50_p95(res.latency_s[r]), len(res.latency_s[r]), points) for r in radii),
            " / ".join(f"{statistics.fmean(res.hits[r]):,.1f}" for r in radii),
            sampled(ms(statistics.median(res.naive_s)), len(res.naive_s), points),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    plans = sorted({res.plan for res in results})
    lines += [
        "",
        "- build: `Store.replace_reference_data`, one transaction into a fresh on-disk store (WAL): the meters "
        "table with its indexes plus the R*Tree. Generating the records is not included. re-sync: the same call "
        "again into the populated store, as every sync after the first; the queries below run on that store.",
        f"- data: the portal's statuses and makes; {METERS_PER_DT} meters per DT, and {FAN_OUT}x fewer nodes at "
        "each network level above it, so the indexed columns have many distinct values, as in a real fleet.",
        f"- near: `Store.search_meters(MeterFilter(near=p, radius_km=r), limit={PAGE_SIZE})` at {points} random "
        f"points in the data's bounding box (~{box_km:,.0f} sq km, meters uniform in it), after one warm-up query; "
        "avg hits is the mean `total`.",
        f"- naive: haversine from p to every meter in a Python list (no SQLite), then the same filter and "
        f"ordering for {max(radii):g} km.",
    ]
    if any(len(s) < points for res in results for s in (*res.latency_s.values(), res.naive_s)):
        lines.append(f"- (n=k): series stopped after k of {points} queries to stay within its {budget_s:g} s budget.")
    lines += [
        f"- query plan: {' | '.join(plans)}",
        "- database size: " + ", ".join(f"{res.size:,}: {res.db_mb:,.1f} MB" for res in results),
        "- hit counts, search_meters vs naive (negative: meters inside the radius that search_meters missed): "
        + "; ".join(consistency(res, radii) for res in results),
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--sizes", type=int, nargs="+", default=[403, 10_000, 100_000, 1_000_000], metavar="N")
    parser.add_argument("--max", type=int, metavar="N", help="skip sizes above N")
    parser.add_argument("--radii", type=float, nargs="+", default=[0.5, 2.0], metavar="KM")
    parser.add_argument("--points", type=int, default=50, help="random query points per size (default: 50)")
    parser.add_argument(
        "--budget", type=float, default=60.0, metavar="S", help="seconds per timed series before it stops early"
    )
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    if args.points < 2:
        parser.error("--points must be at least 2")

    sizes = [n for n in args.sizes if args.max is None or n <= args.max]
    radii = sorted(args.radii)
    rng = random.Random(args.seed)
    points = [(rng.uniform(*LAT_RANGE), rng.uniform(*LNG_RANGE)) for _ in range(args.points)]
    results = [bench_size(n, points, radii, args.budget, args.seed) for n in sizes]
    print(report(results, radii, args.points, args.budget))


if __name__ == "__main__":
    main()
