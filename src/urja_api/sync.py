"""Keeping the local index in step with the portal.

Two kinds of data, two freshness policies:

* **Reference data** (meters, network, locations, transformers) is small, changes rarely
  and is available in bulk. It is re-synced every `sync_interval_s` from the signed
  export; an unchanged export is recognised by its hash and doesn't rewrite anything. If
  the export answers in a way we no longer understand (for example the signing scheme
  changed), the sync falls back to crawling the search listing (~21 budgeted calls) and
  detail pages (not budgeted), keeping each meter's last known coordinates. A transient
  outage just keeps the previous snapshot, as does any sync that looks broken (it would
  lose more than half of the meters, their locations or their transformers' ratings).
  `/v1/status` reports how old the snapshot is.
* **Readings** are per meter and come from a rate-limited endpoint, so they are fetched
  on demand and cached per meter for `readings_ttl_s`. If a refresh fails, the cached
  series is served with `stale: true` instead of an error, quickly: with a cached copy
  the portal gets only a short time budget, and after a failure the copy is served
  straight away for a minute instead of asking again. Requests that queued behind a failed
  fetch share its outcome rather than each repeating it. A payload that can't be read, or
  that would replace a good series with a shorter or blank one, is treated as a failure (such a
  series is accepted once the portal has sent the same one three times running). An
  optional background warm-up refreshes every meter, at a pace inside the portal's rate
  limit and with the full time budget, which is what makes fleet-wide questions possible.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .domain.models import Location
from .domain.network import reconcile
from .domain.normalize import (
    MeterRecord,
    NormalizationError,
    RawReading,
    TransformerRecord,
    meter_from_detail_page,
    meter_from_export,
    readings_from_portal,
    transformer_from_portal,
)
from .domain.readings import detect_interval_minutes, merge_duplicates
from .portal.client import PortalClient
from .portal.errors import (
    PortalAuthError,
    PortalError,
    PortalNotFound,
    PortalProtocolError,
    PortalRateLimited,
    PortalUnavailable,
    RequestQueueFull,
)
from .store import Store

log = logging.getLogger(__name__)

# The widest window the portal accepts; it clamps to the data it has.
HISTORY_START = date(2000, 1, 1)
HISTORY_END = date(2099, 12, 31)
# Refuse data that would shrink what we hold below this share of its current size: a
# half-empty export (or series) is far more likely to be a portal fault than real.
MIN_RETAINED_SHARE = 0.5
# With a cached copy to fall back on, a refresh gets only this long before we serve it...
REFRESH_BUDGET_WITH_CACHE_S = 3.0
# ...and after a failed refresh, the copy is served without asking the portal for this long.
FAILED_REFRESH_BACKOFF_S = 60.0
# While the index is still empty, retry a failed first sync quickly rather than every interval.
FIRST_SYNC_RETRY_S = (5, 120)  # initial, maximum (doubling)
# A manual sync this soon after the last successful one returns that result instead.
MANUAL_SYNC_MIN_INTERVAL_S = 30
# A shorter series than the cached one is refused, until the portal has sent that same
# payload this many times running: by then it is the portal's data, not a glitch.
ACCEPT_SHORTER_SERIES_AFTER = 3
# A series may lose this share of its rows to unparseable timestamps before it is refused.
MAX_UNPARSEABLE_SHARE = 0.1
# The crawl fallback gives up once this many detail pages were unavailable.
CRAWL_ABORT_AFTER = 5


class SyncRejected(Exception):
    """A sync produced data we don't trust enough to replace the current snapshot."""


def utcnow() -> datetime:
    return datetime.now(UTC)


def _digest(*payloads: Any) -> str:
    return hashlib.sha256(json.dumps(payloads, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class _Failure:
    """A meter's last readings fetch, when it failed."""

    fetch: int  # which of the meter's fetch attempts it was
    at: float  # time.monotonic()
    error: PortalError

    @property
    def recent(self) -> bool:
        """Whether to hold off asking the portal again. Our own queue being full says
        nothing about the portal, so that is never held against it."""
        fresh = time.monotonic() < self.at + FAILED_REFRESH_BACKOFF_S
        return fresh and not isinstance(self.error, RequestQueueFull)


class SyncService:
    def __init__(
        self,
        client: PortalClient,
        store: Store,
        *,
        sync_interval_s: int = 900,
        readings_ttl_s: int = 900,
        warm_readings: bool = True,
        warm_interval_s: int = 3600,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.client = client
        self.clock = clock  # every timestamp and age this service records or judges
        self.store = store
        self.sync_interval_s = sync_interval_s
        self.readings_ttl = timedelta(seconds=readings_ttl_s)
        self.warm_readings = warm_readings
        self.warm_interval_s = warm_interval_s
        self._meter_locks: dict[str, asyncio.Lock] = {}
        self._fetches: Counter[str] = Counter()  # meter -> fetch attempts finished so far
        self._failures: dict[str, _Failure] = {}  # meter -> its last fetch, if that failed
        self._refused: dict[str, tuple[str, int]] = {}  # meter -> (payload hash, times refused running)
        self._tasks: list[asyncio.Task[Any]] = []
        self._sync_task: asyncio.Task[dict[str, Any]] | None = None
        self._reference_digest: str | None = None
        self._snapshot_notes: list[str] = []  # what reconciling the current snapshot reported
        self._last_summary: tuple[float, dict[str, Any]] | None = None  # (monotonic, summary)
        self.next_sync_at: float | None = None  # monotonic time of the next scheduled sync
        self.warmup: dict[str, Any] = {"state": "idle", "done": 0, "total": 0, "failed": 0}

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._sync_loop(), name="reference-sync"))
        if self.warm_readings:
            self._tasks.append(asyncio.create_task(self._warm_loop(), name="readings-warm-up"))

    async def stop(self) -> None:
        tasks = [*self._tasks, *([self._sync_task] if self._sync_task else [])]
        for task in tasks:  # a sync started by either loop or by POST /v1/sync runs in its own task
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    async def _sync_loop(self) -> None:
        retry_s = FIRST_SYNC_RETRY_S[0]
        while True:
            try:
                await self.sync_reference_data()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the loop alive; the failure is recorded in sync_runs
                log.exception("reference sync failed")
            if await asyncio.to_thread(self.store.has_reference_data):
                delay, retry_s = self.sync_interval_s, FIRST_SYNC_RETRY_S[0]
            else:  # nothing to serve yet: try again soon
                delay, retry_s = retry_s, min(retry_s * 2, FIRST_SYNC_RETRY_S[1])
            self.next_sync_at = time.monotonic() + delay
            await asyncio.sleep(delay)

    async def _warm_loop(self) -> None:
        while True:
            if await asyncio.to_thread(self.store.has_reference_data):
                try:
                    await self.warm_all_readings()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("readings warm-up failed")
                await asyncio.sleep(self.warm_interval_s)
            else:
                await asyncio.sleep(5)  # wait for the first reference sync

    # ------------------------------------------------------------------ reference data

    async def sync_reference_data(self) -> dict[str, Any]:
        """Run a sync, or join the one already running (concurrent callers share it)."""
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = asyncio.create_task(self._sync(), name="reference-sync-run")
        return await asyncio.shield(self._sync_task)

    async def request_sync(self) -> dict[str, Any]:
        """A manual sync: returns the last result if one succeeded moments ago."""
        idle = self._sync_task is None or self._sync_task.done()
        if idle and self._last_summary and time.monotonic() - self._last_summary[0] < MANUAL_SYNC_MIN_INTERVAL_S:
            return self._last_summary[1]
        return await self.sync_reference_data()

    async def _sync(self) -> dict[str, Any]:
        started = self.clock()
        run_id = await asyncio.to_thread(self.store.start_sync_run, started)
        warnings: list[str] = []
        try:
            dt_rows = await self.client.list_transformers()
            transformers, skipped = self._transformers(dt_rows)
            warnings += skipped
            try:
                export_rows = await self.client.export_meters()
                meters, skipped = self._meters_from_export(export_rows)
                source, digest = "export", _digest(export_rows, dt_rows)
            except PortalProtocolError as exc:
                # The export answered, but not in a way we understand (e.g. signing changed).
                # Outages (PortalUnavailable / rate limits) are *not* a reason to crawl: the
                # current snapshot is kept and the next scheduled sync tries again.
                log.warning("export unusable (%s); falling back to crawling detail pages", exc)
                warnings.append(f"export unusable, used detail-page crawl: {exc}")
                meters, skipped = await self._meters_from_crawl()
                source, digest = "crawl", None
                warnings += await asyncio.to_thread(self._carry_over_locations, meters)
            warnings += skipped
            await asyncio.to_thread(self._check_not_shrinking, meters, transformers)

            if digest is not None and digest == self._reference_digest:
                stored = len(await asyncio.to_thread(self.store.list_transformers))
                await asyncio.to_thread(self.store.touch_reference_data, started)  # checked, not rewritten
                warnings += self._snapshot_notes
                warnings.append("portal data unchanged since the last sync; snapshot kept as is")
            else:
                # Reconciling and rewriting the snapshot is CPU + SQLite work: keep it off
                # the event loop so the API stays responsive during a sync.
                stored, notes = await asyncio.to_thread(self._store_snapshot, meters, transformers, started)
                warnings += notes
                self._reference_digest, self._snapshot_notes = digest, notes
        except asyncio.CancelledError:
            # Shutting down: record the run as finished, without awaiting anything again.
            self.store.finish_sync_run(run_id, self.clock(), "failed", error="cancelled: the service was stopping")
            raise
        except Exception as exc:
            self._last_summary = None  # the manual-sync cool-down only shields a sync that is still the latest
            await asyncio.to_thread(
                self.store.finish_sync_run, run_id, self.clock(), "failed", error=f"{type(exc).__name__}: {exc}"
            )
            raise
        summary = {"source": source, "meters": len(meters), "transformers": stored, "warnings": warnings}
        await asyncio.to_thread(self.store.finish_sync_run, run_id, self.clock(), "ok", **summary)
        self._last_summary = (time.monotonic(), summary)
        log.info("reference sync ok: %s", {k: v for k, v in summary.items() if k != "warnings"})
        return summary

    @staticmethod
    def _transformers(rows: list[dict[str, Any]]) -> tuple[list[TransformerRecord], list[str]]:
        transformers, skipped = [], []
        for row in rows:
            try:
                transformers.append(transformer_from_portal(row))
            except NormalizationError as exc:
                skipped.append(f"skipped transformer record: {exc}")
        return transformers, skipped

    @staticmethod
    def _meters_from_export(rows: list[dict[str, Any]]) -> tuple[list[MeterRecord], list[str]]:
        meters: dict[str, MeterRecord] = {}
        skipped: list[str] = []
        for row in rows:
            try:
                meter = meter_from_export(row)
            except NormalizationError as exc:
                skipped.append(f"skipped export record: {exc}")
                continue
            if meter.meter_id in meters:
                skipped.append(f"skipped a second export record for meter {meter.meter_id}")
                continue
            meters[meter.meter_id] = meter
        return list(meters.values()), skipped

    async def _meters_from_crawl(self) -> tuple[list[MeterRecord], list[str]]:
        """Fallback: search listing for ids, detail page per meter, a few at a time."""
        ids = await self.client.list_meter_ids()
        semaphore = asyncio.Semaphore(4)
        skipped: list[str] = []
        unavailable: list[str] = []

        async def one(meter_id: str) -> MeterRecord | None:
            async with semaphore:
                if len(unavailable) >= CRAWL_ABORT_AFTER:
                    return None  # the pages are down: don't spend a full retry budget on each
                try:
                    return meter_from_detail_page(await self.client.get_meter_page(meter_id))
                except (PortalNotFound, PortalProtocolError, NormalizationError) as exc:
                    # One unreadable page shouldn't sink the whole fallback.
                    skipped.append(f"skipped {meter_id}: {exc}")
                except (PortalUnavailable, PortalRateLimited, RequestQueueFull) as exc:
                    unavailable.append(meter_id)
                    skipped.append(f"skipped {meter_id}: {exc}")
                return None

        results = await asyncio.gather(*(one(i) for i in ids))
        # A page we couldn't reach is not a meter that is gone. With a snapshot to protect,
        # an incomplete crawl keeps it; only a first sync settles for what it could read.
        have_snapshot = await asyncio.to_thread(self.store.has_reference_data)
        if unavailable and (have_snapshot or len(unavailable) >= CRAWL_ABORT_AFTER):
            raise PortalUnavailable(
                f"crawl incomplete: {len(unavailable)} or more detail pages were unavailable "
                f"(first: {unavailable[0]}); keeping the previous snapshot"
            )
        return [m for m in results if m is not None], skipped

    def _carry_over_locations(self, meters: list[MeterRecord]) -> list[str]:
        """Detail pages carry no coordinates: keep each meter's last known location."""
        known = self.store.all_meters()
        missing = 0
        for meter in meters:
            row = known.get(meter.meter_id)
            if meter.location is None and row is not None and row["latitude"] is not None:
                meter.location = Location(latitude=row["latitude"], longitude=row["longitude"])
            missing += meter.location is None
        return [f"{missing} meters have no location (detail pages carry none)"] if missing else []

    def _check_not_shrinking(self, meters: Sequence[MeterRecord], transformers: Sequence[TransformerRecord]) -> None:
        current = self.store.snapshot_counts()
        for what, new, held, minimum in (
            ("meters", len(meters), current["meters"], 1),
            ("meters with a location", sum(m.location is not None for m in meters), current["located"], 0),
        ):
            if new < max(minimum, held * MIN_RETAINED_SHARE):
                raise SyncRejected(f"portal returned {new} {what} (index has {held}); keeping the previous snapshot")
        # The DT list supplies names and ratings: refuse one that rates far fewer of the
        # transformers our meters hang off than the last one did (as shares, so meters
        # moving onto fewer transformers isn't mistaken for a truncated list).
        referenced = {m.transformer_code for m in meters}
        rated = len(referenced & {t.code for t in transformers if t.capacity_kva is not None})
        if current["transformers"] and referenced:
            held_share = current["rated"] / current["transformers"]
            if rated / len(referenced) < held_share * MIN_RETAINED_SHARE:
                raise SyncRejected(
                    f"the portal's DT list rates {rated} of the {len(referenced)} transformers in use "
                    f"(it was {current['rated']} of {current['transformers']}); keeping the previous snapshot"
                )

    def _store_snapshot(
        self, meters: list[MeterRecord], transformers: list[TransformerRecord], synced_at: datetime
    ) -> tuple[int, list[str]]:
        network = reconcile(meters, transformers)
        self.store.replace_reference_data(
            meters=meters,
            paths=network.meter_paths,
            issues=network.meter_issues,
            transformers=transformers,
            transformer_paths=network.transformer_paths,
            name_variants={code: v for (_, code), v in network.name_variants.items()},
            synced_at=synced_at,
        )
        notes = list(network.unresolved)
        unserved = {t.code for t in transformers} - set(network.transformer_paths)
        if unserved:
            # A transformer's position in the network is only known from its meters.
            notes.append(f"{len(unserved)} transformers on the portal's list have no meters and are not served")
        return len(network.transformer_paths), notes

    def reference_status(self) -> dict[str, Any]:
        last_ok = self.store.last_successful_sync()
        runs = self.store.last_sync_runs(1)
        age = None
        if last_ok:
            age = (self.clock() - datetime.fromisoformat(last_ok["finished_at"])).total_seconds()
        return {
            "last_success_at": last_ok["finished_at"] if last_ok else None,
            "age_s": round(age) if age is not None else None,
            "stale": age is None or age > 2 * self.sync_interval_s,
            "source": last_ok["source"] if last_ok else None,
            "last_run": dict(runs[0]) if runs else None,
            "interval_s": self.sync_interval_s,
        }

    def seconds_to_next_sync(self) -> int | None:
        if self.next_sync_at is None:
            return None
        return max(1, round(self.next_sync_at - time.monotonic()))

    # ------------------------------------------------------------------ readings

    def _meter_lock(self, meter_id: str) -> asyncio.Lock:
        return self._meter_locks.setdefault(meter_id, asyncio.Lock())

    async def ensure_readings(
        self, meter_id: str, *, force: bool = False, background: bool = False
    ) -> tuple[datetime | None, bool]:
        """Make sure the cached series for `meter_id` is fresh enough.

        Returns ``(fetched_at, stale)``. Raises `PortalError` only when there is no cached
        copy to fall back on. `background=True` is for the warm-up, where nobody is waiting:
        the refresh gets the client's full time budget and is tried even right after a failure.
        """
        fetches_on_arrival = self._fetches[meter_id]
        fetched_at = await self._fetched_at(meter_id)
        if not force and fetched_at and self.clock() - fetched_at < self.readings_ttl:
            return fetched_at, False  # fresh: no need to queue behind a fetch in progress
        lock = self._meter_lock(meter_id)
        if fetched_at and not background:
            # With a copy to serve, don't wait long behind someone else's fetch either
            # (the warm-up may hold this lock for its whole, much longer, budget).
            try:
                await asyncio.wait_for(lock.acquire(), REFRESH_BUDGET_WITH_CACHE_S)
            except TimeoutError:
                return fetched_at, True
        else:
            await lock.acquire()
        try:
            return await self._refresh(meter_id, fetches_on_arrival, force=force, background=background)
        finally:
            lock.release()

    async def _fetched_at(self, meter_id: str) -> datetime | None:
        info = await asyncio.to_thread(self.store.readings_fetch_info, meter_id)
        return datetime.fromisoformat(info["fetched_at"]) if info else None

    async def _refresh(
        self, meter_id: str, fetches_on_arrival: int, *, force: bool, background: bool
    ) -> tuple[datetime | None, bool]:
        """Fetch and store the series; the caller holds the meter's lock."""
        info = await asyncio.to_thread(self.store.readings_fetch_info, meter_id)
        fetched_at = datetime.fromisoformat(info["fetched_at"]) if info else None
        if not force and fetched_at and self.clock() - fetched_at < self.readings_ttl:
            return fetched_at, False  # whoever held the lock before us refreshed it
        if (failure := self._failures.get(meter_id)) and not force and not background:
            # Share the outcome of a fetch we queued behind; and for a minute after the
            # portal failed, serve the copy without asking again.
            queued_behind_it = failure.fetch > fetches_on_arrival
            if fetched_at and (queued_behind_it or failure.recent):
                return fetched_at, True
            if fetched_at is None and queued_behind_it:
                raise failure.error
        try:
            rows = await self.client.get_meter_energy(
                meter_id,
                HISTORY_START,
                HISTORY_END,
                max_wait_s=REFRESH_BUDGET_WITH_CACHE_S if info and not background else None,
            )
            readings = self._checked_series(meter_id, rows, info)
        except PortalNotFound:
            raise
        except PortalError as exc:
            self._fetches[meter_id] += 1
            self._failures[meter_id] = _Failure(self._fetches[meter_id], time.monotonic(), exc)
            if fetched_at is None:
                raise
            log.warning("serving cached readings for %s: %s", meter_id, exc)
            return fetched_at, True
        self._fetches[meter_id] += 1
        self._failures.pop(meter_id, None)
        readings, duplicates = merge_duplicates(readings)
        now = self.clock()
        interval = detect_interval_minutes([r.timestamp for r in readings])
        await asyncio.to_thread(
            self.store.replace_readings,
            meter_id,
            readings,
            duplicates,
            now,
            interval,
            content_hash=_digest(rows),
        )
        return now, False

    def _checked_series(self, meter_id: str, rows: list[dict[str, Any]], cached: Any) -> list[RawReading]:
        """Parse a series, refusing one we can't read or that would replace good cached data
        with less. The first is format drift and stays refused. The second may be a real
        correction, so it is accepted once the portal keeps sending that same payload.

        A register that is merely blank is not drift: such a series is served as it is, with
        its `missing_kwh` flags, unless it would replace cached values.
        """
        readings = readings_from_portal(rows)
        unreadable = len(rows) - len(readings)
        if rows and not readings:
            raise PortalProtocolError(f"none of the {len(rows)} readings for {meter_id} could be parsed")
        if unreadable > 1 and unreadable > len(rows) * MAX_UNPARSEABLE_SHARE:
            raise PortalProtocolError(
                f"{unreadable} of the {len(rows)} readings for {meter_id} have an unreadable timestamp"
            )
        horizon = self.clock() + timedelta(days=1)
        if readings and readings[-1].timestamp > horizon:  # a glitch row must not become "the latest reading"
            kept = [r for r in readings if r.timestamp <= horizon]
            log.warning("ignoring %d readings dated in the future for %s", len(readings) - len(kept), meter_id)
            readings = kept
        no_kwh = bool(readings) and all(r.energy_kwh is None for r in readings)
        sent_blank = all("kwh" in row and not str(row["kwh"] if row["kwh"] is not None else "").strip() for row in rows)
        if no_kwh and not sent_blank:  # the key is gone or its values no longer parse: drift
            raise PortalProtocolError(f"none of the {len(readings)} readings for {meter_id} has a readable kWh value")

        if cached is not None and cached["reading_count"]:
            truncated = len(readings) < cached["reading_count"] * MIN_RETAINED_SHARE
            ends_earlier = not readings or readings[-1].timestamp.isoformat() < (cached["last_ts"] or "")
            blanked = no_kwh and bool(cached["has_kwh"])  # a register that was already blank loses nothing
            if truncated or ends_earlier or blanked:
                payload = _digest(rows)
                seen, times = self._refused.get(meter_id, ("", 0))
                times = times + 1 if seen == payload else 1
                if times < ACCEPT_SHORTER_SERIES_AFTER:
                    self._refused[meter_id] = (payload, times)
                    what = "only blank kWh values" if blanked else f"{len(readings)} readings"
                    raise PortalProtocolError(
                        f"portal returned {what} for {meter_id} "
                        f"(cache holds {cached['reading_count']} up to {cached['last_ts']}); keeping the cache"
                    )
                log.warning("accepting the lesser series for %s: the portal sent it %d times running", meter_id, times)
        self._refused.pop(meter_id, None)
        return readings

    async def warm_all_readings(self) -> None:
        """Refresh every meter's readings once, paced by the client's rate limiter."""
        ids = await asyncio.to_thread(self.store.meter_ids)
        self.warmup = {"state": "running", "done": 0, "total": len(ids), "failed": 0, "started_at": self.clock()}
        errors: Counter[str] = Counter()
        for meter_id in ids:
            # If the portal has paused us (429), wait it out rather than letting every
            # remaining meter fail fast against the pause.
            await self.client.wait_for_budget()
            error: str | None = None
            try:
                _, stale = await self.ensure_readings(meter_id, background=True)
                if stale:  # the refresh failed and the cached copy was kept
                    failure = self._failures.get(meter_id)
                    error = type(failure.error).__name__ if failure else "RefreshFailed"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = type(exc).__name__
            if error is not None:
                self.warmup["failed"] += 1
                errors[error] += 1
            if error == PortalAuthError.__name__:  # every other meter would fail the same way
                self.warmup.update(state="aborted", finished_at=self.clock(), errors=dict(errors))
                log.error("readings warm-up aborted: the portal refused our login")
                return
            self.warmup["done"] += 1
        self.warmup.update(state="done", finished_at=self.clock(), errors=dict(errors))
