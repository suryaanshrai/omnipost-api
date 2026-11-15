"""Connector registry: slug -> Connector instance. Every platform is a plain
Python class in connectors/platforms/, registered below."""

from __future__ import annotations

from .base import Connector

_registry: dict[str, Connector] = {}


class UnknownConnector(LookupError):
    pass


def register(connector: Connector) -> None:
    """Registers a connector instance under its slug."""
    _registry[connector.slug] = connector


def get(slug: str) -> Connector:
    try:
        return _registry[slug]
    except KeyError:
        raise UnknownConnector(f"No connector registered for slug '{slug}'") from None


def all_slugs() -> list[str]:
    return sorted(_registry.keys())


def _register_hand_written() -> None:
    from .platforms.bluesky import BlueskyConnector
    from .platforms.discord import DiscordConnector
    from .platforms.facebook import FacebookConnector
    from .platforms.instagram import InstagramConnector
    from .platforms.linkedin import LinkedInConnector
    from .platforms.mastodon import MastodonConnector
    from .platforms.telegram import TelegramConnector
    from .platforms.threads import ThreadsConnector
    from .platforms.tiktok import TikTokConnector
    from .platforms.x import XConnector
    from .platforms.youtube import YouTubeConnector

    register(BlueskyConnector())
    register(DiscordConnector())
    register(TelegramConnector())
    register(MastodonConnector())
    register(InstagramConnector())
    register(FacebookConnector())
    register(ThreadsConnector())
    register(LinkedInConnector())
    register(XConnector())
    register(TikTokConnector())
    register(YouTubeConnector())


_register_hand_written()
