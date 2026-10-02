"""Request signing for the portal's bulk export endpoint.

Reverse-engineered from the portal's Transformers page bundle (the "Export all meters"
button). The browser fetches a secret from ``GET /portal/keys`` and signs::

    message   = "\\n".join([METHOD, PATH, QUERY, TIMESTAMP])
    signature = hex(HMAC_SHA256(key=secret_utf8, msg=message_utf8))

sent as ``x-timestamp`` (unix *seconds*) and ``x-signature`` headers. ``QUERY`` is the raw
query string without the leading ``?`` and must match the URL byte for byte. The server
accepts timestamps within about ±4 minutes of its own clock and does not reject replays.
"""

from __future__ import annotations

import hashlib
import hmac


def sign_request(secret: str, method: str, path: str, query: str, timestamp: int) -> str:
    message = "\n".join([method.upper(), path, query, str(timestamp)])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def signature_headers(secret: str, method: str, path: str, query: str, timestamp: int) -> dict[str, str]:
    return {
        "x-timestamp": str(timestamp),
        "x-signature": sign_request(secret, method, path, query, timestamp),
    }
