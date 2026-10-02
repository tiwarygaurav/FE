"""HMAC signing of the bulk export request."""

from __future__ import annotations

import hashlib
import hmac

from urja_api.portal.signing import sign_request, signature_headers

SECRET = "urja-test-secret"
TIMESTAMP = 1782830400  # 2026-06-30T14:40:00Z
# Computed independently with Node's crypto (the portal's own bundle signs in the browser):
# createHmac("sha256", SECRET).update("GET\n/portal/export\npage=1\n1782830400").digest("hex")
KNOWN_SIGNATURE = "d7b3cd2702cd5d277d568546204794d7f10847bca7fa8aec6f08e52bf7670bf3"


def test_known_vector():
    assert sign_request(SECRET, "GET", "/portal/export", "page=1", TIMESTAMP) == KNOWN_SIGNATURE


def test_message_layout():
    message = f"GET\n/portal/export\npage=1\n{TIMESTAMP}".encode()
    expected = hmac.new(SECRET.encode(), message, hashlib.sha256).hexdigest()
    assert sign_request(SECRET, "get", "/portal/export", "page=1", TIMESTAMP) == expected


def test_every_part_is_signed():
    base = sign_request(SECRET, "GET", "/portal/export", "page=1", TIMESTAMP)
    assert sign_request(SECRET, "GET", "/portal/export", "page=2", TIMESTAMP) != base
    assert sign_request(SECRET, "GET", "/portal/export", "page=1", TIMESTAMP + 1) != base
    assert sign_request("other-secret", "GET", "/portal/export", "page=1", TIMESTAMP) != base


def test_headers_carry_seconds_and_lowercase_hex():
    assert signature_headers(SECRET, "GET", "/portal/export", "page=1", TIMESTAMP) == {
        "x-timestamp": "1782830400",
        "x-signature": KNOWN_SIGNATURE,
    }
