"""Client-side throttling for the portal's rate-limited endpoints.

The portal allows ~120 requests per 60 s window across all its ``/portal/*`` JSON
endpoints and answers 429 with no ``Retry-After`` header. Rather than discovering the
limit by hitting it, we pace ourselves with a token bucket set below the observed limit,
and on a 429 anyway (another client sharing the account, a changed limit) we pause *all*
throttled traffic, not just the request that got rejected.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable


class TokenBucket:
    def __init__(
        self,
        rate_per_second: float,
        capacity: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        if rate_per_second <= 0 or capacity < 1:
            raise ValueError("rate_per_second must be > 0 and capacity >= 1")
        self.rate = rate_per_second
        self.capacity = capacity
        self._clock = clock
        self._sleep = sleep
        self._tokens = float(capacity)
        self._updated = clock()
        self._paused_until = 0.0
        self._lock = asyncio.Lock()
        self.waiting = 0  # callers queued in acquire()

    def _refill(self, now: float) -> None:
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def acquire(self) -> None:
        """Wait until a request may be sent. Requests are served in FIFO order."""
        self.waiting += 1
        try:
            async with self._lock:
                while True:
                    now = self._clock()
                    if now < self._paused_until:
                        await self._sleep(self._paused_until - now)
                        continue
                    self._refill(now)
                    if self._tokens >= 1:
                        self._tokens -= 1
                        return
                    await self._sleep((1 - self._tokens) / self.rate)
        finally:
            self.waiting -= 1

    def try_acquire(self) -> bool:
        """Take a token only if one is free right now and nobody is already queued."""
        now = self._clock()
        if self._lock.locked() or now < self._paused_until:
            return False
        self._refill(now)
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True

    def pause(self, seconds: float) -> None:
        """Stop handing out tokens for `seconds` (called after the portal answers 429)."""
        now = self._clock()
        self._paused_until = max(self._paused_until, now + seconds)
        self._tokens = 0.0
        self._updated = now

    @property
    def paused_for(self) -> float:
        return max(0.0, self._paused_until - self._clock())
