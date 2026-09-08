"""Facebook Pages, via the Graph API. OAuth is Facebook Login (see
meta_oauth.py); publishing itself needs no polling — a feed/photo/video post
resolves synchronously — so this is a one-step publish() unlike Instagram's
container flow, but still hand-written (not a declarative spec) because
picking the right endpoint per post_kind and carrying the Page access token
(distinct from the user token OAuth hands back) needs real code.
"""

from __future__ import annotations

import requests

from ..base import ChannelCredentials, Connector, OAuthAuthorization, PublishContext, PublishOutcome, PublishState
from ..capabilities import Capabilities, MediaRule
from ..errors import PermanentError
from . import meta_oauth

_SCOPES = "pages_show_list,pages_read_engagement,pages_manage_posts"


class FacebookConnector(Connector):
    slug = "facebook"
    capabilities = Capabilities(
        slug="facebook",
        display_name="Facebook Pages",
        max_text_length=63206,
        supports_alt_text=True,
        post_kinds=("text", "image", "video"),
        media=(
            MediaRule(kinds=("image",), max_count=10, max_size_mb=10, allowed_mime_types=("image/jpeg", "image/png")),
            MediaRule(kinds=("video",), max_count=1, max_size_mb=10240, max_duration_s=240 * 60),
        ),
    )

    def authorize_url(self, redirect_uri: str, state: str, **extra: str) -> OAuthAuthorization:
        url = meta_oauth.build_authorize_url(redirect_uri, state, _SCOPES)
        return OAuthAuthorization(authorize_url=url, state=state)

    def exchange_code(self, code: str, redirect_uri: str, **extra: str) -> ChannelCredentials:
        short_token = meta_oauth.exchange_code_for_user_token(code, redirect_uri)
        long_token = meta_oauth.exchange_for_long_lived_token(short_token)
        pages = meta_oauth.list_pages(long_token)
        if not pages:
            raise PermanentError("This Facebook account doesn't manage any Pages OmniPost can post to.")
        # A user managing several Pages picks which one via `extra["page_id"]`
        # (surfaced by the frontend after a first call that lists pages);
        # absent that, the first Page keeps a single-Page workspace frictionless.
        page_id = extra.get("page_id")
        page = next((p for p in pages if p["id"] == page_id), pages[0]) if page_id else pages[0]
        return ChannelCredentials(
            values={"PAGE_ACCESS_TOKEN": page["access_token"], "PAGE_ID": page["id"]},
            external_account_id=page["id"],
        )

    def refresh(self, credentials: ChannelCredentials) -> ChannelCredentials:
        # Page access tokens derived from a long-lived user token effectively
        # don't expire on their own (they're invalidated by password change
        # or app revocation, which surfaces as AuthExpired on the next
        # publish) — nothing to proactively refresh.
        return credentials

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        page_id = ctx.credentials["PAGE_ID"]
        token = ctx.credentials["PAGE_ACCESS_TOKEN"]

        if ctx.post_kind == "video" and ctx.media:
            endpoint, payload = f"{page_id}/videos", {
                "file_url": ctx.media[0].url,
                "description": ctx.text,
                "access_token": token,
            }
        elif ctx.post_kind == "image" and ctx.media:
            endpoint, payload = f"{page_id}/photos", {
                "url": ctx.media[0].url,
                "caption": ctx.text,
                "access_token": token,
            }
        else:
            payload = {"message": ctx.text, "access_token": token}
            if ctx.link:
                payload["link"] = ctx.link
            endpoint = f"{page_id}/feed"

        response = requests.post(f"{meta_oauth.GRAPH_BASE}/{endpoint}", data=payload, timeout=60)
        if not response.ok:
            meta_oauth.raise_graph_error(response, action="publishing to Facebook")

        remote_id = response.json().get("id") or response.json().get("post_id", "")
        permalink = f"https://www.facebook.com/{remote_id}" if remote_id else None
        return PublishOutcome(done=True, remote_id=remote_id, permalink=permalink)
