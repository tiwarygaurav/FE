"""Shared fixtures: real portal data and a `FakePortal` built from it.

``fixtures/`` holds a curated subset of a real portal snapshot, copied verbatim: 20 meters
on 11 transformers, all 40 DT rows, the meters' energy series (half-hourly ones trimmed to
1–5 June) and three raw ``__data.json`` detail pages. The meters were picked for the
portal's quirks:

* J100000: half-hourly, legacy nameplate, decommissioned;
* J100004: v2 nameplate, faulty;
* J100011 / J100153: blank circle / substation *code*; J100162 / J100218: blank feeder /
  circle *name*;
* J100400–J100402: stale DT-007 name "Old Malviya Nagar Xfmr" (J100006 has "Sanganer DT 7");
* J100089: daily, with the portal's blank duplicate row on 30/06;
* the rest are ordinary meters, some on the same transformers as the quirky ones (so blanks
  can be repaired from the DT's other meters) and some putting D-01 under three circles.
"""

from __future__ import annotations

import copy
import functools
import json
from collections.abc import AsyncIterator, Callable
from email.utils import formatdate
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fake_portal import BASE_URL, EMAIL, PASSWORD, FakePortal

from urja_api.domain.normalize import (
    MeterRecord,
    TransformerRecord,
    meter_from_export,
    readings_from_portal,
    transformer_from_portal,
)
from urja_api.domain.readings import StoredReading, merge_duplicates
from urja_api.portal import client as client_module
from urja_api.portal.client import PortalClient

FIXTURES = Path(__file__).parent / "fixtures"


@functools.cache
def _parsed(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def load_fixture(name: str) -> Any:
    """A fresh copy of a fixture file, so tests can inject faults without leaking them."""
    return copy.deepcopy(_parsed(name))


def stored_readings(rows: list[dict[str, str]]) -> list[StoredReading]:
    """Portal energy rows as the store serves them: parsed, with duplicates merged and flagged."""
    readings, duplicates = merge_duplicates(readings_from_portal(rows))
    return [
        StoredReading(r.timestamp, r.energy_kwh, r.energy_kvah, r.voltage_v, duplicates.get(r.timestamp, 0))
        for r in readings
    ]


def with_date_header(app: Any, clock: Callable[[], float]) -> Any:
    """Wrap an ASGI app so responses carry the `Date` header the real portal sends, from `clock`."""

    async def wrapped(scope: Any, receive: Any, send: Any) -> None:
        async def send_with_date(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                date = formatdate(clock(), usegmt=True).encode()
                message = {**message, "headers": [*message.get("headers", []), (b"date", date)]}
            await send(message)

        await app(scope, receive, send_with_date)

    return wrapped


# ----------------------------------------------------------------------------- data


@pytest.fixture
def fixture_json() -> Callable[[str], Any]:
    """Load any file under fixtures/, e.g. the raw detail pages."""
    return load_fixture


@pytest.fixture
def export_records() -> list[dict[str, Any]]:
    return load_fixture("export_subset.json")


@pytest.fixture
def dt_rows() -> list[dict[str, Any]]:
    return load_fixture("dts.json")


@pytest.fixture
def energy(export_records: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """Raw `/energy` rows per meter id."""
    return {r["meterId"]: load_fixture(f"energy/{r['meterId']}.json") for r in export_records}


@pytest.fixture
def meters(export_records: list[dict[str, Any]]) -> list[MeterRecord]:
    return [meter_from_export(r) for r in export_records]


@pytest.fixture
def transformers(dt_rows: list[dict[str, Any]]) -> list[TransformerRecord]:
    return [transformer_from_portal(r) for r in dt_rows]


@pytest.fixture
def stored(energy: dict[str, list[dict[str, str]]]) -> Callable[[str], list[StoredReading]]:
    """A fixture meter's readings as the store serves them."""
    return lambda meter_id: stored_readings(energy[meter_id])


# ----------------------------------------------------------------------------- fake portal


@pytest.fixture
def make_portal(
    export_records: list[dict[str, Any]], dt_rows: list[dict[str, Any]], energy: dict[str, list[dict[str, str]]]
) -> Callable[..., FakePortal]:
    """Build a `FakePortal` serving the fixture data; keyword arguments set its knobs."""

    def make(**knobs: Any) -> FakePortal:
        return FakePortal(
            meters=copy.deepcopy(export_records),
            transformers=copy.deepcopy(dt_rows),
            energy=copy.deepcopy(energy),
            **knobs,
        )

    return make


@pytest.fixture
def fake_portal(make_portal: Callable[..., FakePortal]) -> FakePortal:
    return make_portal()


@pytest.fixture
async def connect() -> AsyncIterator[Callable[..., PortalClient]]:
    """Build `PortalClient`s talking to a `FakePortal` in-process; they are closed at teardown.

    The base URL must be the fake's https one, or httpx would drop the portal's `Secure` cookie.
    """
    clients: list[PortalClient] = []

    def factory(
        portal: FakePortal,
        *,
        date_header: bool = False,
        base_url: str = BASE_URL,
        password: str = PASSWORD,
        **options: Any,
    ) -> PortalClient:
        app = with_date_header(portal.app, portal.clock) if date_header else portal.app
        client = PortalClient(base_url, EMAIL, password, transport=httpx.ASGITransport(app=app), **options)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.aclose()


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scale the client's jittered backoff (429 pause, 5xx delay) down 1000x: retries take milliseconds."""
    monkeypatch.setattr(client_module, "random", SimpleNamespace(uniform=lambda low, high: low / 1000))
