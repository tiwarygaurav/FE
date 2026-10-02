"""Exceptions raised by the portal client.

They describe *what went wrong upstream* in terms the API layer can map onto HTTP
responses (404 / 502 / 503 / 504) without knowing anything about the portal's formats.
"""

from __future__ import annotations


class PortalError(Exception):
    """Base class for all portal failures."""


class PortalAuthError(PortalError):
    """Login was rejected (bad credentials, CSRF/Origin rejection, unexpected login response)."""


class PortalNotFound(PortalError):
    """The portal says the requested entity does not exist."""


class PortalRateLimited(PortalError):
    """The portal kept answering 429 until our retry deadline ran out."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RequestQueueFull(PortalError):
    """Our own pacing queue for the portal's rate budget is backed up (no 429 involved)."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PortalUnavailable(PortalError):
    """Network errors, timeouts or 5xx responses that persisted through retries."""


class PortalProtocolError(PortalError):
    """The portal answered with something we don't understand (format drift)."""
