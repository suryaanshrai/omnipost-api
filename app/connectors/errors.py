"""Error taxonomy every connector maps upstream failures onto.

The old system stored raw `response.text` into a user-visible Notification
field (omnipost_api/models.py send_request()), which can echo OAuth tokens
back into the UI, and treated every failure the same way: log it and stop.
Different failures need different responses — a rate limit should back off
and retry, an expired token should trigger reconnection, a policy violation
should never be retried. This module is what lets the job engine (publisher.py)
make that distinction without knowing anything about the platform.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorClass(StrEnum):
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_EXPIRED = "AUTH_EXPIRED"
    INVALID_MEDIA = "INVALID_MEDIA"
    DUPLICATE = "DUPLICATE"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"

    @property
    def retryable(self) -> bool:
        return self in {ErrorClass.RATE_LIMITED, ErrorClass.TRANSIENT}


class ConnectorError(Exception):
    """Base for all connector-raised errors. Never carries a raw response body
    in a field that might be surfaced to a user verbatim — use `safe_detail`
    for that, and keep `raw_detail` for logs only (which are secret-scrubbed
    by app.logging_filters.RedactSecretsFilter)."""

    error_class: ErrorClass = ErrorClass.PERMANENT

    def __init__(self, safe_detail: str, *, raw_detail: str | None = None, retry_after: float | None = None):
        super().__init__(safe_detail)
        self.safe_detail = safe_detail
        self.raw_detail = raw_detail or safe_detail
        self.retry_after = retry_after


class RateLimited(ConnectorError):
    error_class = ErrorClass.RATE_LIMITED


class AuthExpired(ConnectorError):
    error_class = ErrorClass.AUTH_EXPIRED


class InvalidMedia(ConnectorError):
    error_class = ErrorClass.INVALID_MEDIA


class DuplicatePost(ConnectorError):
    error_class = ErrorClass.DUPLICATE


class PolicyViolation(ConnectorError):
    error_class = ErrorClass.POLICY_VIOLATION


class TransientError(ConnectorError):
    error_class = ErrorClass.TRANSIENT


class PermanentError(ConnectorError):
    error_class = ErrorClass.PERMANENT


@dataclass(frozen=True)
class Finding:
    """A single validation result surfaced to the composer (Phase 3) or raised
    as a blocking error at publish time. `code` is a stable machine-readable
    identifier; `message` is what the user sees."""

    code: str
    message: str
    blocking: bool = True
    field: str | None = None
