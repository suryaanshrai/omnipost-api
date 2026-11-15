"""Turning a platform's HTTP error into the right ConnectorError, shared by
connectors whose platforms use plain status codes."""

from __future__ import annotations

import requests

from .errors import AuthExpired, PermanentError, RateLimited, TransientError


def retry_after_seconds(response: requests.Response, default: float = 30.0) -> float:
    header = response.headers.get("Retry-After", "")
    return float(header) if header.isdigit() else default


def raise_for_platform(
    response: requests.Response,
    platform: str,
    *,
    action: str = "publishing",
    auth_statuses: tuple[int, ...] = (401, 403),
) -> None:
    """No-op for a 2xx. Otherwise: auth failures need a reconnect, 429 waits
    for Retry-After, 5xx and timeouts retry, any other 4xx is permanent (the
    request itself is wrong and retrying won't fix it)."""
    if response.ok:
        return
    detail = response.text[:500]
    status = response.status_code
    if status in auth_statuses:
        raise AuthExpired(f"{platform} rejected this channel's credentials.", raw_detail=detail)
    if status == 429:
        raise RateLimited(
            f"{platform} rate-limited {action}.", raw_detail=detail, retry_after=retry_after_seconds(response)
        )
    if status >= 500 or status == 408:
        raise TransientError(f"{platform} had a problem {action} ({status}).", raw_detail=detail)
    raise PermanentError(f"{platform} rejected this post ({status}).", raw_detail=detail)


def json_or_empty(response: requests.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
