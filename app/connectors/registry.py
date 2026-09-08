"""Connector registry: slug -> Connector instance.

Declarative connectors (specs/*.yaml) are loaded and validated once at import
time — a malformed spec fails fast at process start, not on some user's first
publish attempt. Hand-written connectors (needed for platforms with real
state machines: media processing, chunked upload, OAuth token exchange) are
registered explicitly below.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .base import Connector
from .declarative import DeclarativeConnector, SpecError, validate_spec

_SPECS_DIR = Path(__file__).parent / "specs"

_registry: dict[str, Connector] = {}


class UnknownConnector(LookupError):
    pass


def _load_declarative_specs() -> None:
    if not _SPECS_DIR.exists():
        return
    for path in sorted(_SPECS_DIR.glob("*.yaml")):
        with open(path, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        try:
            validate_spec(spec)
        except SpecError as exc:
            raise SpecError(f"{path.name}: {exc}") from exc
        _registry[spec["slug"]] = DeclarativeConnector(spec=spec)


def register(connector: Connector) -> None:
    """Registers a hand-written connector instance, overriding any
    declarative spec with the same slug (hand-written always wins)."""
    _registry[connector.slug] = connector


def get(slug: str) -> Connector:
    try:
        return _registry[slug]
    except KeyError:
        raise UnknownConnector(f"No connector registered for slug '{slug}'") from None


def all_slugs() -> list[str]:
    return sorted(_registry.keys())


def _register_hand_written() -> None:
    # Imported here (not at module scope) so a syntax/import error in one
    # hand-written connector doesn't prevent every declarative spec from
    # loading — each import is independent and reported clearly.
    from .platforms.bluesky import BlueskyConnector
    from .platforms.facebook import FacebookConnector
    from .platforms.instagram import InstagramConnector
    from .platforms.linkedin import LinkedInConnector
    from .platforms.mastodon import MastodonConnector
    from .platforms.threads import ThreadsConnector
    from .platforms.tiktok import TikTokConnector
    from .platforms.x import XConnector
    from .platforms.youtube import YouTubeConnector

    register(BlueskyConnector())
    register(MastodonConnector())
    register(InstagramConnector())
    register(FacebookConnector())
    register(ThreadsConnector())
    register(LinkedInConnector())
    register(XConnector())
    register(TikTokConnector())
    register(YouTubeConnector())


_load_declarative_specs()
_register_hand_written()
