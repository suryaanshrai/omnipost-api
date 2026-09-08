"""Instagram (feed, Reels, Stories, carousel), via the Instagram Graph API.

Every post — even a single image — goes through a two-step "container"
protocol: create a media container (POST .../media), then publish it
(POST .../media_publish). Video-backed containers (feed video, Reels, video
Stories) process asynchronously on Meta's side and must be polled for
status_code == "FINISHED" before publish will succeed; images are ready
immediately. That's exactly the resumable state machine PublishState exists
for — this is the connector the whole "state machine, not N independently
scheduled steps" design in connectors/base.py was written for.

Carousels create one child container per media item (`is_carousel_item`)
then a parent CAROUSEL container referencing the children. Scope cut: child
containers are treated as instant (no per-child status poll) — true for
image children, and true in practice for the great majority of video
carousel items too; a video child that's still processing when the parent
publish fires surfaces as a normal Meta error on that call rather than a
silent partial carousel, so nothing is lost silently.

OAuth: Facebook Login (see meta_oauth.py) plus the `instagram_business_account`
field on /me/accounts, which is how a Facebook Page's linked IG professional
account is discovered — Instagram itself has no separate OAuth dialog.
"""

from __future__ import annotations

import requests

from ..base import (
    ChannelCredentials,
    Connector,
    MediaRef,
    OAuthAuthorization,
    PublishContext,
    PublishOutcome,
    PublishState,
)
from ..capabilities import Capabilities, MediaRule
from ..errors import PermanentError
from . import meta_oauth

_SCOPES = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement"


class InstagramConnector(Connector):
    slug = "instagram"
    capabilities = Capabilities(
        slug="instagram",
        display_name="Instagram",
        max_text_length=2200,
        supports_alt_text=True,
        post_kinds=("image", "video", "story", "short_video"),
        media=(
            MediaRule(
                kinds=("image",),
                max_count=10,
                max_size_mb=8,
                allowed_aspect_ratios=("4:5", "1:1", "1.91:1"),
                allowed_mime_types=("image/jpeg",),
            ),
            MediaRule(kinds=("video",), max_count=10, max_size_mb=4096, max_duration_s=3600, min_duration_s=3),
        ),
    )

    # --- OAuth ---

    def authorize_url(self, redirect_uri: str, state: str, **extra: str) -> OAuthAuthorization:
        url = meta_oauth.build_authorize_url(redirect_uri, state, _SCOPES)
        return OAuthAuthorization(authorize_url=url, state=state)

    def exchange_code(self, code: str, redirect_uri: str, **extra: str) -> ChannelCredentials:
        short_token = meta_oauth.exchange_code_for_user_token(code, redirect_uri)
        long_token = meta_oauth.exchange_for_long_lived_token(short_token)
        pages = meta_oauth.list_pages(long_token)
        linked = [p for p in pages if p.get("instagram_business_account")]
        if not linked:
            raise PermanentError(
                "None of this Facebook user's Pages have a linked Instagram professional account."
            )
        page_id = extra.get("page_id")
        page = next((p for p in linked if p["id"] == page_id), linked[0]) if page_id else linked[0]
        ig_user_id = page["instagram_business_account"]["id"]
        return ChannelCredentials(
            values={"PAGE_ACCESS_TOKEN": page["access_token"], "IG_USER_ID": ig_user_id},
            external_account_id=ig_user_id,
        )

    def refresh(self, credentials: ChannelCredentials) -> ChannelCredentials:
        return credentials

    # --- Publish state machine: start -> [processing]* -> done ---

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        ig_user_id = ctx.credentials["IG_USER_ID"]
        token = ctx.credentials["PAGE_ACCESS_TOKEN"]

        if state.step == "start":
            return self._start(ctx, ig_user_id, token)
        if state.step == "processing":
            return self._poll(state, ig_user_id, token)
        raise PermanentError(f"Unknown Instagram publish state '{state.step}'.")

    def _start(self, ctx: PublishContext, ig_user_id: str, token: str) -> PublishOutcome:
        if len(ctx.media) > 1:
            children = [self._create_child_container(ig_user_id, token, item) for item in ctx.media[:10]]
            creation_id = self._create_container(
                ig_user_id, token, media_type="CAROUSEL", caption=ctx.text, children=children
            )
            return self._publish_container(ig_user_id, token, creation_id)

        media_item = ctx.media[0]
        media_type = self._media_type_for(ctx.post_kind, media_item)
        creation_id = self._create_container(
            ig_user_id, token, media_type=media_type, caption=ctx.text, media_item=media_item
        )

        if media_item.kind == "video":
            return PublishOutcome(
                done=False,
                state=PublishState(step="processing", data={"creation_id": creation_id}),
                retry_after_s=5,
            )
        return self._publish_container(ig_user_id, token, creation_id)

    def _poll(self, state: PublishState, ig_user_id: str, token: str) -> PublishOutcome:
        creation_id = state.data["creation_id"]
        response = requests.get(
            f"{meta_oauth.GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=15,
        )
        if not response.ok:
            meta_oauth.raise_graph_error(response, action="checking Instagram media status")

        status_code = response.json().get("status_code", "IN_PROGRESS")
        if status_code == "FINISHED":
            return self._publish_container(ig_user_id, token, creation_id)
        if status_code == "ERROR":
            raise PermanentError("Instagram failed to process this video.")
        return PublishOutcome(done=False, state=state, retry_after_s=5)

    def _media_type_for(self, post_kind: str, media_item: MediaRef) -> str:
        if post_kind == "story":
            return "STORIES"
        if post_kind == "short_video":
            return "REELS"
        return "VIDEO" if media_item.kind == "video" else "IMAGE"

    def _create_child_container(self, ig_user_id: str, token: str, media_item: MediaRef) -> str:
        payload: dict[str, str] = {"access_token": token, "is_carousel_item": "true"}
        if media_item.kind == "video":
            payload["media_type"] = "VIDEO"
            payload["video_url"] = media_item.url
        else:
            payload["image_url"] = media_item.url
        response = requests.post(f"{meta_oauth.GRAPH_BASE}/{ig_user_id}/media", data=payload, timeout=60)
        if not response.ok:
            meta_oauth.raise_graph_error(response, action="creating an Instagram carousel item")
        return response.json()["id"]

    def _create_container(
        self,
        ig_user_id: str,
        token: str,
        *,
        media_type: str,
        caption: str,
        media_item: MediaRef | None = None,
        children: list[str] | None = None,
    ) -> str:
        payload: dict[str, str] = {"access_token": token, "caption": caption}
        if media_type == "CAROUSEL":
            payload["media_type"] = "CAROUSEL"
            payload["children"] = ",".join(children or [])
        else:
            if media_type != "IMAGE":
                payload["media_type"] = media_type
            assert media_item is not None
            if media_item.kind == "video":
                payload["video_url"] = media_item.url
            else:
                payload["image_url"] = media_item.url
        response = requests.post(f"{meta_oauth.GRAPH_BASE}/{ig_user_id}/media", data=payload, timeout=60)
        if not response.ok:
            meta_oauth.raise_graph_error(response, action="creating an Instagram media container")
        return response.json()["id"]

    def _publish_container(self, ig_user_id: str, token: str, creation_id: str) -> PublishOutcome:
        response = requests.post(
            f"{meta_oauth.GRAPH_BASE}/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": token},
            timeout=30,
        )
        if not response.ok:
            meta_oauth.raise_graph_error(response, action="publishing an Instagram post")

        media_id = response.json()["id"]
        permalink = self._fetch_permalink(media_id, token)
        return PublishOutcome(done=True, remote_id=media_id, permalink=permalink)

    def _fetch_permalink(self, media_id: str, token: str) -> str | None:
        response = requests.get(
            f"{meta_oauth.GRAPH_BASE}/{media_id}", params={"fields": "permalink", "access_token": token}, timeout=15
        )
        return response.json().get("permalink") if response.ok else None
