"""The client-side token bucket, driven by a fake clock (no real sleeping)."""

from __future__ import annotations

import asyncio

import pytest

from urja_api.portal.ratelimit import TokenBucket


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds
        await asyncio.sleep(0)  # yield to other tasks, like a real sleep would


def make_bucket(rate: float, capacity: int) -> tuple[TokenBucket, FakeClock]:
    clock = FakeClock()
    return TokenBucket(rate, capacity, clock=clock, sleep=clock.sleep), clock


async def test_a_burst_goes_out_immediately():
    bucket, clock = make_bucket(rate=2, capacity=5)
    for _ in range(5):
        await bucket.acquire()
    assert clock.sleeps == []


async def test_after_the_burst_requests_are_paced_at_the_rate():
    bucket, clock = make_bucket(rate=2, capacity=5)
    for _ in range(5 + 10):
        await bucket.acquire()
    assert clock.sleeps == [0.5] * 10
    assert clock.now == pytest.approx(1_000 + 10 / 2)


async def test_idle_time_refills_up_to_capacity_only():
    bucket, clock = make_bucket(rate=1, capacity=3)
    for _ in range(3):
        await bucket.acquire()
    clock.now += 3_600
    for _ in range(3):
        await bucket.acquire()
    assert clock.sleeps == []
    await bucket.acquire()
    assert clock.sleeps == [1.0]


async def test_pause_holds_back_every_request():
    bucket, clock = make_bucket(rate=10, capacity=5)
    bucket.pause(60)
    assert bucket.paused_for == 60
    clock.now += 20
    assert bucket.paused_for == 40
    await bucket.acquire()
    assert clock.sleeps == [40.0]
    assert bucket.paused_for == 0


async def test_pause_empties_the_bucket():
    bucket, clock = make_bucket(rate=0.5, capacity=5)
    bucket.pause(1)
    await bucket.acquire()
    # the pause itself, then the rest of a token at 0.5 tokens/s
    assert clock.sleeps == [1.0, 1.0]


def test_a_shorter_pause_does_not_cut_a_longer_one_short():
    bucket, _ = make_bucket(rate=1, capacity=1)
    bucket.pause(30)
    bucket.pause(5)
    assert bucket.paused_for == 30


async def test_waiters_are_served_in_arrival_order():
    bucket, clock = make_bucket(rate=1, capacity=1)
    served: list[int] = []

    async def request(n: int) -> None:
        await bucket.acquire()
        served.append(n)

    await asyncio.gather(*(request(n) for n in range(5)))
    assert served == [0, 1, 2, 3, 4]
    assert clock.sleeps == [1.0] * 4


@pytest.mark.parametrize(("rate", "capacity"), [(0, 1), (-1, 5), (1, 0)])
def test_invalid_configuration(rate, capacity):
    with pytest.raises(ValueError):
        TokenBucket(rate, capacity)
