"""Bluesky (AT Protocol), hand-written rather than declarative because it
needs a session token minted from an app password, then (for image posts) a
raw-bytes blob upload before the post record itself can reference it — not a
single templated JSON request.

Auth: app password (identifier + app-specific password from Bluesky account
settings), not OAuth. This is the currently-supported, simpler auth method;
full OAuth (with DPoP) is a newer, heavier flow this connector doesn't need
for a first correct implementation.
"""

from __future__ import annotations

import re
from datetime import UTC
from typing import Any

import requests

from ..base import ChannelCredentials, Connector, PublishContext, PublishOutcome, PublishState
from ..capabilities import Capabilities, MediaRule
from ..errors import AuthExpired, InvalidMedia, PermanentError, RateLimited, TransientError

_BASE_URL = "https://bsky.social/xrpc"
_MAX_GRAPHEMES = 300  # Bluesky's limit is graphemes, not bytes/chars; len() undercounts
# emoji/combining sequences but never overcounts, so it's a safe (if slightly
# lenient) approximation without a full grapheme-segmentation dependency.

# Link facets only for now — @mention facets need an extra
# com.atproto.identity.resolveHandle call per mention to turn "@handle" into
# a DID, which isn't implemented yet. Mentions still post fine as plain text,
# they just won't render as tappable links.
_URL_RE = re.compile(r"https?://[^\s]+")


class BlueskyConnector(Connector):
    slug = "bluesky"
    capabilities = Capabilities(
        slug="bluesky",
        display_name="Bluesky",
        max_text_length=_MAX_GRAPHEMES,
        supports_alt_text=True,
        post_kinds=("text", "image"),
        media=(
            MediaRule(kinds=("image",), max_count=4, max_size_mb=2, allowed_mime_types=("image/jpeg", "image/png")),
        ),
    )

    def _create_session(self, credentials: ChannelCredentials) -> dict:
        response = requests.post(
            f"{_BASE_URL}/com.atproto.server.createSession",
            json={"identifier": credentials["IDENTIFIER"], "password": credentials["APP_PASSWORD"]},
            timeout=15,
        )
        if response.status_code == 401:
            raise AuthExpired(
                "Bluesky rejected the identifier/app password.", raw_detail=response.text[:500]
            )
        if response.status_code == 429:
            raise RateLimited("Bluesky rate-limited session creation.", retry_after=30)
        if not response.ok:
            raise TransientError(
                f"Bluesky session creation failed ({response.status_code}).", raw_detail=response.text[:500]
            )
        return response.json()

    def _upload_blob(self, access_jwt: str, image_bytes: bytes, mime_type: str) -> dict:
        response = requests.post(
            f"{_BASE_URL}/com.atproto.repo.uploadBlob",
            headers={"Authorization": f"Bearer {access_jwt}", "Content-Type": mime_type},
            data=image_bytes,
            timeout=30,
        )
        if response.status_code == 400:
            raise InvalidMedia("Bluesky rejected the image.", raw_detail=response.text[:500])
        if not response.ok:
            raise TransientError(
                f"Bluesky blob upload failed ({response.status_code}).", raw_detail=response.text[:500]
            )
        return response.json()["blob"]

    def _build_facets(self, text: str) -> list[dict]:
        """Bluesky renders links/mentions from byte-offset "facets", not
        markdown or auto-linkification — a plain-text URL posted without a
        facet just sits there as text."""
        facets = []
        for match in _URL_RE.finditer(text):
            start = len(text[: match.start()].encode("utf-8"))
            end = len(text[: match.end()].encode("utf-8"))
            facets.append(
                {
                    "index": {"byteStart": start, "byteEnd": end},
                    "features": [{"$type": "app.bsky.richtext.facet#link", "uri": match.group(0)}],
                }
            )
        return facets

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        data = dict(state.data)

        if state.step == "start":
            session = self._create_session(ctx.credentials)
            data["access_jwt"] = session["accessJwt"]
            data["did"] = session["did"]
            state = PublishState(step="session_ready", data=data)

        images = []
        if ctx.media:
            if state.step == "session_ready":
                blobs = []
                for media_item in ctx.media[:4]:
                    fetched = requests.get(media_item.url, timeout=30)
                    if not fetched.ok:
                        raise TransientError(f"Could not fetch media from {media_item.url} for upload.")
                    blob = self._upload_blob(data["access_jwt"], fetched.content, media_item.mime_type)
                    blobs.append({"blob": blob, "alt": media_item.alt_text or ""})
                data["image_blobs"] = blobs
                state = PublishState(step="blobs_uploaded", data=data)
            images = data.get("image_blobs", [])

        record: dict = {
            "$type": "app.bsky.feed.post",
            "text": ctx.text,
            "createdAt": _now_iso(),
        }
        facets = self._build_facets(ctx.text)
        if facets:
            record["facets"] = facets
        if images:
            record["embed"] = {"$type": "app.bsky.embed.images", "images": images}

        response = requests.post(
            f"{_BASE_URL}/com.atproto.repo.createRecord",
            headers={"Authorization": f"Bearer {data['access_jwt']}"},
            json={"repo": data["did"], "collection": "app.bsky.feed.post", "record": record},
            timeout=15,
        )
        if response.status_code == 401:
            raise AuthExpired("Bluesky session expired mid-publish.", raw_detail=response.text[:500])
        if response.status_code == 429:
            raise RateLimited("Bluesky rate-limited post creation.", retry_after=30)
        if not response.ok:
            raise PermanentError(f"Bluesky rejected the post ({response.status_code}).", raw_detail=response.text[:500])

        body = response.json()
        uri = body.get("uri", "")
        # at://did:plc:xxx/app.bsky.feed.post/<rkey> -> bsky.app permalink
        rkey = uri.rsplit("/", 1)[-1] if uri else ""
        handle = ctx.credentials.get("IDENTIFIER", data["did"])
        permalink = f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else ""

        return PublishOutcome(done=True, remote_id=uri, permalink=permalink)

    def fetch_metrics(self, credentials: ChannelCredentials, remote_id: str) -> dict[str, Any]:
        """remote_id is the at:// record URI publish() returned. getPostThread
        with depth=0 is the cheapest call that still returns the engagement
        counts (a dedicated counts-only endpoint doesn't exist in the API)."""
        session = self._create_session(credentials)
        response = requests.get(
            f"{_BASE_URL}/app.bsky.feed.getPostThread",
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            params={"uri": remote_id, "depth": "0"},
            timeout=15,
        )
        if response.status_code == 401:
            raise AuthExpired("Bluesky rejected the identifier/app password.", raw_detail=response.text[:500])
        if not response.ok:
            return {}  # e.g. the post was deleted — nothing to report, not an error

        post = response.json().get("thread", {}).get("post") or {}
        if not post:
            return {}
        return {
            "likes": post.get("likeCount"),
            "shares": post.get("repostCount"),
            "comments": post.get("replyCount"),
            "impressions": None,  # Bluesky's public API doesn't expose view/impression counts
        }


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
