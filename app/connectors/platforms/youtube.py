"""YouTube Shorts, via the YouTube Data API v3's resumable upload protocol
(the "Video" tier's other platform — see tiktok.py for the sibling
connector). Scoped to Shorts specifically (vertical, <=60s), not general
long-form YouTube video, matching the revamp plan's Video tier.

Resumable upload is a three-step handoff: POST the video's metadata to get a
one-time upload session URL back in a `Location` header, PUT the actual
bytes to that URL, then poll the resulting video resource's
`status.uploadStatus` until YouTube finishes transcoding ("processed") —
same yield-and-re-enqueue shape as Instagram/Threads/TikTok's container
polls, just with an upload step in front of it instead of a URL handoff.

Managed app (YOUTUBE_CLIENT_ID/YOUTUBE_CLIENT_SECRET) via standard Google
OAuth2 with `access_type=offline` to get a refresh token up front.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import requests

from ..base import (
    ChannelCredentials,
    Connector,
    OAuthAuthorization,
    PublishContext,
    PublishOutcome,
    PublishState,
)
from ..capabilities import Capabilities, MediaRule
from ..errors import AuthExpired, InvalidMedia, PermanentError, RateLimited, TransientError

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_UPLOAD_INIT_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
_API_BASE = "https://www.googleapis.com/youtube/v3"
_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def _client_id() -> str:
    value = os.environ.get("YOUTUBE_CLIENT_ID")
    if not value:
        raise PermanentError("YOUTUBE_CLIENT_ID is not configured on this server.")
    return value


def _client_secret() -> str:
    value = os.environ.get("YOUTUBE_CLIENT_SECRET")
    if not value:
        raise PermanentError("YOUTUBE_CLIENT_SECRET is not configured on this server.")
    return value


class YouTubeConnector(Connector):
    slug = "youtube"
    capabilities = Capabilities(
        slug="youtube",
        display_name="YouTube Shorts",
        max_text_length=5000,
        post_kinds=("short_video",),
        media=(
            MediaRule(
                kinds=("video",),
                max_count=1,
                max_size_mb=1024 * 10,
                max_duration_s=60,
                min_duration_s=1,
                allowed_aspect_ratios=("9:16",),
            ),
        ),
    )

    # --- OAuth ---

    def authorize_url(self, redirect_uri: str, state: str, **extra: str) -> OAuthAuthorization:
        params = {
            "client_id": _client_id(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return OAuthAuthorization(authorize_url=f"{_AUTHORIZE_URL}?{query}", state=state)

    def exchange_code(self, code: str, redirect_uri: str, **extra: str) -> ChannelCredentials:
        response = requests.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        if not response.ok:
            raise PermanentError("Google rejected the authorization code.", raw_detail=response.text[:500])
        token_data = response.json()
        access_token = token_data["access_token"]

        channel = requests.get(
            f"{_API_BASE}/channels",
            params={"part": "id", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        items = channel.json().get("items", []) if channel.ok else []
        channel_id = items[0]["id"] if items else ""

        return ChannelCredentials(
            values={"ACCESS_TOKEN": access_token, "REFRESH_TOKEN": token_data.get("refresh_token", "")},
            external_account_id=channel_id,
        )

    def refresh(self, credentials: ChannelCredentials) -> ChannelCredentials:
        refresh_token = credentials.get("REFRESH_TOKEN")
        if not refresh_token:
            return credentials
        response = requests.post(
            _TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        if not response.ok:
            raise AuthExpired("Google rejected this channel's refresh token.", raw_detail=response.text[:500])
        return ChannelCredentials(
            values={**credentials.values, "ACCESS_TOKEN": response.json()["access_token"]},
            external_account_id=credentials.external_account_id,
        )

    # --- Publish: start (initiate + upload) -> processing -> done ---

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        if state.step == "start":
            return self._start(ctx)
        if state.step == "processing":
            return self._poll(ctx, state)
        raise PermanentError(f"Unknown YouTube publish state '{state.step}'.")

    def _start(self, ctx: PublishContext) -> PublishOutcome:
        if not ctx.media:
            raise PermanentError("YouTube Shorts requires a video.")
        token = ctx.credentials["ACCESS_TOKEN"]
        item = ctx.media[0]

        fetched = requests.get(item.url, timeout=120)
        if not fetched.ok:
            raise TransientError(f"Could not fetch media from {item.url} for upload.")
        content = fetched.content

        title = ctx.extra.get("title") or (ctx.text[:95] or "Untitled")
        init = requests.post(
            f"{_UPLOAD_INIT_URL}?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": item.mime_type or "video/mp4",
                "X-Upload-Content-Length": str(len(content)),
            },
            json={
                "snippet": {"title": title, "description": ctx.text, "categoryId": "22"},
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
            },
            timeout=30,
        )
        if not init.ok:
            self._raise_error(init, action="initiating a YouTube upload")
        upload_url = init.headers.get("Location")
        if not upload_url:
            raise TransientError("YouTube did not return an upload session URL.")

        upload = requests.put(
            upload_url, data=content, headers={"Content-Type": item.mime_type or "video/mp4"}, timeout=300
        )
        if not upload.ok:
            raise InvalidMedia("YouTube rejected the video upload.", raw_detail=upload.text[:500])

        video_id = upload.json()["id"]
        return PublishOutcome(
            done=False, state=PublishState(step="processing", data={"video_id": video_id}), retry_after_s=5
        )

    def _poll(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        token = ctx.credentials["ACCESS_TOKEN"]
        video_id = state.data["video_id"]

        response = requests.get(
            f"{_API_BASE}/videos",
            params={"part": "status", "id": video_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if not response.ok:
            self._raise_error(response, action="checking a YouTube video's status")

        items = response.json().get("items", [])
        upload_status = items[0]["status"]["uploadStatus"] if items else "uploaded"
        if upload_status == "processed":
            return PublishOutcome(
                done=True, remote_id=video_id, permalink=f"https://www.youtube.com/shorts/{video_id}"
            )
        if upload_status in ("failed", "rejected"):
            raise PermanentError(f"YouTube {upload_status} this video.")
        return PublishOutcome(done=False, state=state, retry_after_s=5)

    def _raise_error(self, response: requests.Response, *, action: str) -> None:
        if response.status_code == 401:
            raise AuthExpired("YouTube rejected this channel's credentials.", raw_detail=response.text[:500])
        if response.status_code in (429, 403):
            raise RateLimited(f"YouTube rate-limited {action}.", raw_detail=response.text[:500], retry_after=60)
        raise TransientError(f"YouTube returned {response.status_code} while {action}.", raw_detail=response.text[:500])
