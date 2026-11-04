"""Guards for requests to user-supplied addresses (SSRF).

A user can make the server fetch a URL (AI repurpose `source_url`), post to a
webhook they pasted (Discord), or talk to an instance they named (Mastodon).
Without a check, those can point at the server's own network: Redis, Postgres,
the cloud metadata service at 169.254.169.254.

Limit: the check resolves the host before the request is made, so a DNS
record that changes between the check and the request (DNS rebinding) can
still slip through. Upgrade to a resolving transport adapter if that matters.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit

import requests

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.IGNORECASE)


class UnsafeURL(ValueError):
    pass


def _is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_host(host: str) -> None:
    """Raise UnsafeURL if `host` is, or resolves to, a non-public address.
    A name that doesn't resolve is allowed through: the request itself will
    fail, and there's nothing internal to reach."""
    host = host.strip("[]")
    try:
        if not _is_public_ip(host):
            raise UnsafeURL("That address points at a private network.")
        return
    except ValueError as exc:
        if isinstance(exc, UnsafeURL):
            raise
    if host.lower() in ("localhost",) or host.lower().endswith((".localhost", ".internal", ".local")):
        raise UnsafeURL("That address points at a private network.")
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return
    for info in infos:
        if not _is_public_ip(str(info[4][0])):
            raise UnsafeURL("That address points at a private network.")


def assert_public_url(url: str, *, schemes: tuple[str, ...] = ("https", "http")) -> None:
    parts = urlsplit(url)
    if parts.scheme not in schemes or not parts.hostname:
        raise UnsafeURL(f"Only {' or '.join(schemes)} addresses are allowed.")
    if parts.username or parts.password:
        raise UnsafeURL("Addresses with embedded credentials aren't allowed.")
    assert_public_host(parts.hostname)


def assert_public_domain(domain: str) -> None:
    """A bare hostname (e.g. a Mastodon instance), no scheme, path or port."""
    if not _HOSTNAME_RE.match(domain or ""):
        raise UnsafeURL("Enter a domain name like mastodon.social.")
    assert_public_host(domain)


def safe_get(url: str, *, timeout: float, max_bytes: int = 2_000_000, max_redirects: int = 3, **kwargs):
    """requests.get that checks every redirect hop and caps the body size."""
    for _ in range(max_redirects + 1):
        assert_public_url(url)
        response = requests.get(url, timeout=timeout, allow_redirects=False, stream=True, **kwargs)
        if response.is_redirect and response.headers.get("location"):
            url = urljoin(url, response.headers["location"])
            response.close()
            continue
        body = b""
        for chunk in response.iter_content(64 * 1024):
            body += chunk
            if len(body) > max_bytes:
                response.close()
                raise UnsafeURL("That page is too large to read.")
        response._content = body
        return response
    raise UnsafeURL("Too many redirects.")
