"""RSS/Atom feeds to drafts. Each new item becomes a draft for the feed's
channels; nothing is published without a person sending it.

Parsing uses the standard library. Python's bundled expat refuses entity
expansion attacks ("billion laughs"), and the fetch is size-capped.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from connectors.netguard import UnsafeURL, safe_get
from django.utils import timezone
from omnipost_api.models import Feed, Post, PostTarget

logger = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"
_MAX_SEEN = 500  # remember this many item ids per feed


def parse_items(xml_text: str) -> list[dict[str, str]]:
    """[{id, title, link}], newest first as the feed lists them."""
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):  # RSS 2.0
        link = (item.findtext("link") or "").strip()
        items.append(
            {
                "id": (item.findtext("guid") or link).strip(),
                "title": (item.findtext("title") or "").strip(),
                "link": link,
            }
        )
    for entry in root.iter(f"{_ATOM}entry"):  # Atom
        link_el = entry.find(f"{_ATOM}link[@rel='alternate']") or entry.find(f"{_ATOM}link")
        link = (link_el.get("href") if link_el is not None else "") or ""
        items.append(
            {
                "id": (entry.findtext(f"{_ATOM}id") or link).strip(),
                "title": (entry.findtext(f"{_ATOM}title") or "").strip(),
                "link": link.strip(),
            }
        )
    return [i for i in items if i["id"]]


def check_feed(feed: Feed) -> int:
    """Returns the number of drafts created."""
    first_run = feed.last_checked_at is None
    feed.last_checked_at = timezone.now()
    try:
        response = safe_get(feed.url, timeout=20, headers={"User-Agent": "OmniPost/1.0"})
        response.raise_for_status()
        items = parse_items(response.text)
    except (UnsafeURL, ET.ParseError, OSError, ValueError) as exc:
        feed.last_error = f"Couldn't read the feed: {exc}"[:300]
        feed.save(update_fields=["last_checked_at", "last_error"])
        return 0
    except Exception as exc:  # noqa: BLE001 - requests errors, HTTP errors
        feed.last_error = f"Couldn't fetch the feed ({type(exc).__name__})."
        feed.save(update_fields=["last_checked_at", "last_error"])
        return 0

    seen = list(feed.seen_ids or [])
    new_items = [i for i in items if i["id"] not in seen]
    created = 0
    channels = list(feed.channels.all())
    # On the first check, only remember what's there: importing a feed's
    # whole back catalogue as drafts would bury the Drafts screen.
    if not first_run:
        for item in reversed(new_items):
            text = feed.template.replace("{title}", item["title"]).replace("{link}", item["link"]).strip()
            post = Post.objects.create(workspace=feed.workspace, kind="text", base_text=text, link=item["link"])
            for channel in channels:
                PostTarget.objects.create(post=post, channel=channel)
            created += 1
    feed.seen_ids = ([i["id"] for i in new_items] + seen)[:_MAX_SEEN]
    feed.last_error = ""
    feed.save(update_fields=["seen_ids", "last_checked_at", "last_error"])
    return created


def check_all_feeds() -> dict[str, int]:
    drafts = 0
    for feed in Feed.objects.filter(is_active=True).select_related("workspace"):
        drafts += check_feed(feed)
    return {"drafts": drafts}
