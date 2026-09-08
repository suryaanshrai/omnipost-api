"""Retry backoff. Exponential with a cap, unless the platform told us exactly
how long to wait (e.g. a 429's Retry-After header, surfaced via
ConnectorError.retry_after)."""

from __future__ import annotations

MAX_RETRIES = 5
BASE_DELAY_S = 30
MAX_DELAY_S = 3600
DEFAULT_POLL_DELAY_S = 5  # for non-error "still processing, check back" outcomes


def backoff_seconds(attempt_number: int, retry_after: float | None = None) -> float:
    if retry_after is not None:
        return max(retry_after, 1.0)
    return min(BASE_DELAY_S * (2 ** (attempt_number - 1)), MAX_DELAY_S)
