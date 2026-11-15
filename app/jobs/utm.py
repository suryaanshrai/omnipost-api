"""UTM tagging for a post's link, when the workspace has it switched on."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def tag(link: str, *, source: str, medium: str = "social", campaign: str = "omnipost") -> str:
    """Adds utm_source/medium/campaign, keeping any the link already has."""
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https"):
        return link
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in (("utm_source", source), ("utm_medium", medium), ("utm_campaign", campaign)):
        query.setdefault(key, value)
    return urlunsplit(parts._replace(query=urlencode(query)))
