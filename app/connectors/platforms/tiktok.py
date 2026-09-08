"""TikTok, via the Content Posting API's PULL_FROM_URL upload mode: TikTok's
servers fetch the video from a URL we give them (the same URL-based handoff
Instagram/Facebook/Threads use) rather than us streaming bytes to TikTok
ourselves — no chunked upload code needed here, unlike LinkedIn/X.

Managed app (TIKTOK_CLIENT_KEY/TIKTOK_CLIENT_SECRET) — TikTok's audit gate
(see the revamp plan's "platform approvals" workstream) means posts are
`SELF_ONLY` (visible only to the poster, for testing) until the Content
Posting API audit passes; `privacy_level` defaults to that and is only
overridable via `ctx.extra["privacy_level"]` once a workspace is confirmed
past the audit, rather than defaulting to public and hoping.

Publish is init -> poll: init returns a `publish_id` immediately while
TikTok downloads and processes the video in the background; the state
machine polls `.../status/fetch/` until PUBLISH_COMPLETE or FAILED.
"""

from __future__ import annotations

from urllib.parse import quote

import requests

from ..base import ChannelCredentials, Connector, OAuthAuthorization, PublishContext, PublishOutcome, PublishState
from ..capabilities import Capabilities, MediaRule
from ..errors import AuthExpired, PermanentError, RateLimited, TransientError

_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
_API_BASE = "https://open.tiktokapis.com/v2"
_SCOPES = "user.info.basic,video.publish,video.upload"
_DEFAULT_PRIVACY_LEVEL = "SELF_ONLY"


def _client_key() -> str:
    import os

    value = os.environ.get("TIKTOK_CLIENT_KEY")
    if not value:
        raise PermanentError("TIKTOK_CLIENT_KEY is not configured on this server.")
    return value


def _client_secret() -> str:
    import os

    value = os.environ.get("TIKTOK_CLIENT_SECRET")
    if not value:
        raise PermanentError("TIKTOK_CLIENT_SECRET is not configured on this server.")
    return value


class TikTokConnector(Connector):
    slug = "tiktok"
    capabilities = Capabilities(
        slug="tiktok",
        display_name="TikTok",
        max_text_length=2200,
        post_kinds=("short_video",),
        media=(
            MediaRule(
                kinds=("video",),
                max_count=1,
                max_size_mb=4096,
                max_duration_s=600,
                min_duration_s=3,
                allowed_aspect_ratios=("9:16",),
            ),
        ),
    )

    # --- OAuth ---
    # TikTok's `state` param is returned unmodified on the callback, so
    # (unlike Mastodon/X) no signed-state wrapping is needed to carry extra
    # bookkeeping through the redirect.

    def authorize_url(self, redirect_uri: str, state: str, **extra: str) -> OAuthAuthorization:
        params = {
            "client_key": _client_key(),
            "response_type": "code",
            "scope": _SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return OAuthAuthorization(authorize_url=f"{_AUTHORIZE_URL}?{query}", state=state)

    def exchange_code(self, code: str, redirect_uri: str, **extra: str) -> ChannelCredentials:
        response = requests.post(
            _TOKEN_URL,
            data={
                "client_key": _client_key(),
                "client_secret": _client_secret(),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if not response.ok:
            raise PermanentError("TikTok rejected the authorization code.", raw_detail=response.text[:500])
        token_data = response.json()
        return ChannelCredentials(
            values={
                "ACCESS_TOKEN": token_data["access_token"],
                "REFRESH_TOKEN": token_data.get("refresh_token", ""),
            },
            external_account_id=token_data.get("open_id", ""),
        )

    def refresh(self, credentials: ChannelCredentials) -> ChannelCredentials:
        refresh_token = credentials.get("REFRESH_TOKEN")
        if not refresh_token:
            return credentials
        response = requests.post(
            _TOKEN_URL,
            data={
                "client_key": _client_key(),
                "client_secret": _client_secret(),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        if not response.ok:
            raise AuthExpired("TikTok rejected this channel's refresh token.", raw_detail=response.text[:500])
        token_data = response.json()
        return ChannelCredentials(
            values={
                **credentials.values,
                "ACCESS_TOKEN": token_data["access_token"],
                "REFRESH_TOKEN": token_data.get("refresh_token", refresh_token),
            },
            external_account_id=credentials.external_account_id,
        )

    # --- Publish: start -> processing -> done ---

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        if state.step == "start":
            return self._start(ctx)
        if state.step == "processing":
            return self._poll(ctx, state)
        raise PermanentError(f"Unknown TikTok publish state '{state.step}'.")

    def _start(self, ctx: PublishContext) -> PublishOutcome:
        if not ctx.media:
            raise PermanentError("TikTok requires a video.")
        token = ctx.credentials["ACCESS_TOKEN"]
        privacy_level = ctx.extra.get("privacy_level", _DEFAULT_PRIVACY_LEVEL)

        response = requests.post(
            f"{_API_BASE}/post/publish/video/init/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "post_info": {
                    "title": ctx.text,
                    "privacy_level": privacy_level,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {"source": "PULL_FROM_URL", "video_url": ctx.media[0].url},
            },
            timeout=30,
        )
        if not response.ok:
            self._raise_error(response, action="starting a TikTok upload")

        publish_id = response.json()["data"]["publish_id"]
        return PublishOutcome(
            done=False, state=PublishState(step="processing", data={"publish_id": publish_id}), retry_after_s=5
        )

    def _poll(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        token = ctx.credentials["ACCESS_TOKEN"]
        publish_id = state.data["publish_id"]

        response = requests.post(
            f"{_API_BASE}/post/publish/status/fetch/",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"publish_id": publish_id},
            timeout=15,
        )
        if not response.ok:
            self._raise_error(response, action="checking a TikTok upload's status")

        data = response.json()["data"]
        status = data.get("status")
        if status == "PUBLISH_COMPLETE":
            post_ids = data.get("publicaly_available_post_id") or []
            remote_id = str(post_ids[0]) if post_ids else publish_id
            permalink = f"https://www.tiktok.com/@me/video/{remote_id}" if post_ids else None
            return PublishOutcome(done=True, remote_id=remote_id, permalink=permalink)
        if status == "FAILED":
            reason = data.get("fail_reason", "unknown reason")
            raise PermanentError(f"TikTok failed to publish this video: {reason}.")
        return PublishOutcome(done=False, state=state, retry_after_s=5)

    def _raise_error(self, response: requests.Response, *, action: str) -> None:
        if response.status_code == 401:
            raise AuthExpired("TikTok rejected this channel's credentials.", raw_detail=response.text[:500])
        if response.status_code == 429:
            raise RateLimited(f"TikTok rate-limited {action}.", raw_detail=response.text[:500], retry_after=60)
        raise TransientError(f"TikTok returned {response.status_code} while {action}.", raw_detail=response.text[:500])
