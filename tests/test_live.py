"""Smoke test against the real portal.

Opt-in, because it logs in with real credentials and spends a little of the portal's
shared /portal/* budget (three calls: keys, export, one energy series). Credentials come
from URJA_PORTAL_EMAIL / URJA_PORTAL_PASSWORD in the environment or `.env`, as for the
service itself:

    URJA_LIVE_TESTS=1 uv run pytest -m live
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from pydantic import ValidationError

from urja_api.config import Settings
from urja_api.domain.normalize import IST, meter_from_detail_page, meter_from_export, readings_from_portal
from urja_api.portal.client import PortalClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("URJA_LIVE_TESTS") != "1", reason="live portal tests are opt-in: URJA_LIVE_TESTS=1"
    ),
]


@pytest.fixture
async def portal() -> AsyncIterator[PortalClient]:
    try:
        settings = Settings()  # type: ignore[call-arg]  # credentials come from the environment
    except ValidationError:
        pytest.skip("URJA_PORTAL_EMAIL and URJA_PORTAL_PASSWORD must be set (environment or .env)")
    client = PortalClient(settings.portal_base_url, settings.portal_email, settings.portal_password.get_secret_value())
    yield client
    await client.aclose()


async def test_live_portal_smoke(portal):
    exported = {m.meter_id: m for m in map(meter_from_export, await portal.export_meters())}
    assert len(exported) == 403
    assert portal.stats.logins == 1

    readings = readings_from_portal(await portal.get_meter_energy("J100000"))  # the portal's default window
    assert readings
    assert all(r.timestamp.tzinfo is IST for r in readings)
    assert readings[-1].energy_kwh is not None and readings[-1].energy_kwh >= readings[0].energy_kwh

    detail = meter_from_detail_page(await portal.get_meter_page("J100000"))
    assert detail.serial_number == exported["J100000"].serial_number
    assert detail.transformer_code == exported["J100000"].transformer_code
