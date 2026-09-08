"""LinkedIn, via the versioned Posts API (api.linkedin.com/rest/posts).

`requires_own_app = True`: LinkedIn's Community Management API — the tier
needed to post as a member on a schedule — requires a registered legal
entity and a two-tier app review that a single OmniPost-managed app can't
absorb for every customer. Each workspace registers its own LinkedIn app
(client_id/client_secret) via AppCredential; the oauth service injects them
into `extra` automatically (see omnipost_api/services/oauth.py), so this
connector just reads `extra["client_id"]`/`extra["client_secret"]` like any
other caller-supplied value.

Image/video posts need a separate upload step before the post itself:
initializeUpload -> PUT bytes -> (video only) finalizeUpload -> poll until
AVAILABLE -> create the post referencing the resulting asset urn. Only the
video path needs the poll — LinkedIn processes images synchronously.
"""

from __future__ import annotations

from urllib.parse import quote

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
from ..errors import AuthExpired, InvalidMedia, PermanentError, RateLimited, TransientError

_AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
_API_BASE = "https://api.linkedin.com"
_API_VERSION = "202401"
_SCOPES = "openid profile w_member_social"


class LinkedInConnector(Connector):
    slug = "linkedin"
    requires_own_app = True
    capabilities = Capabilities(
        slug="linkedin",
        display_name="LinkedIn",
        max_text_length=3000,
        supports_alt_text=False,
        post_kinds=("text", "image", "video"),
        media=(
            MediaRule(kinds=("image",), max_count=1, max_size_mb=10),
            MediaRule(kinds=("video",), max_count=1, max_size_mb=5120, max_duration_s=600, min_duration_s=3),
        ),
    )

    # --- OAuth ---

    def authorize_url(self, redirect_uri: str, state: str, **extra: str) -> OAuthAuthorization:
        client_id = extra.get("client_id")
        if not client_id:
            raise PermanentError("LinkedIn requires this workspace's own app client_id.")
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": _SCOPES,
        }
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return OAuthAuthorization(authorize_url=f"{_AUTHORIZE_URL}?{query}", state=state)

    def exchange_code(self, code: str, redirect_uri: str, **extra: str) -> ChannelCredentials:
        client_id = extra.get("client_id")
        client_secret = extra.get("client_secret")
        if not client_id or not client_secret:
            raise PermanentError("LinkedIn requires this workspace's own app client_id/client_secret.")

        response = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=15,
        )
        if not response.ok:
            raise PermanentError("LinkedIn rejected the authorization code.", raw_detail=response.text[:500])
        access_token = response.json()["access_token"]

        userinfo = requests.get(_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
        if not userinfo.ok:
            raise TransientError("Could not fetch this LinkedIn member's profile.", raw_detail=userinfo.text[:500])
        member_id = userinfo.json()["sub"]

        return ChannelCredentials(
            values={"ACCESS_TOKEN": access_token, "MEMBER_URN": f"urn:li:person:{member_id}"},
            external_account_id=member_id,
        )

    def refresh(self, credentials: ChannelCredentials) -> ChannelCredentials:
        # No refresh token on LinkedIn's standard tier — a 60-day access
        # token expiry means a full re-auth, which surfaces as AuthExpired.
        return credentials

    # --- Publish ---

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        if state.step == "start":
            return self._start(ctx, state)
        if state.step == "processing":
            return self._poll_video(ctx, state)
        raise PermanentError(f"Unknown LinkedIn publish state '{state.step}'.")

    def _start(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        author = ctx.credentials["MEMBER_URN"]
        token = ctx.credentials["ACCESS_TOKEN"]

        if ctx.media and ctx.media[0].kind == "video":
            video_urn = self._upload_video(author, token, ctx.media[0])
            return PublishOutcome(
                done=False,
                state=PublishState(step="processing", data={"media_urn": video_urn, "text": ctx.text}),
                retry_after_s=5,
            )

        media_urn: str | None = self._upload_image(author, token, ctx.media[0]) if ctx.media else None
        return self._create_post(author, token, ctx.text, media_urn)

    def _poll_video(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        token = ctx.credentials["ACCESS_TOKEN"]
        author = ctx.credentials["MEMBER_URN"]
        media_urn = state.data["media_urn"]

        response = requests.get(
            f"{_API_BASE}/rest/videos/{quote(media_urn, safe='')}", headers=self._headers(token), timeout=15
        )
        if not response.ok:
            self._raise_error(response, action="checking LinkedIn video status")
        video_status = response.json().get("status")
        if video_status == "AVAILABLE":
            return self._create_post(author, token, state.data["text"], media_urn)
        if video_status == "PROCESSING_FAILED":
            raise PermanentError("LinkedIn failed to process this video.")
        return PublishOutcome(done=False, state=state, retry_after_s=5)

    def _headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "LinkedIn-Version": _API_VERSION,
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json",
        }

    def _fetch_bytes(self, url: str) -> bytes:
        fetched = requests.get(url, timeout=60)
        if not fetched.ok:
            raise TransientError(f"Could not fetch media from {url} for upload.")
        return fetched.content

    def _upload_image(self, author: str, token: str, item: MediaRef) -> str:
        init = requests.post(
            f"{_API_BASE}/rest/images?action=initializeUpload",
            headers=self._headers(token),
            json={"initializeUploadRequest": {"owner": author}},
            timeout=30,
        )
        if not init.ok:
            self._raise_error(init, action="initializing a LinkedIn image upload")
        value = init.json()["value"]

        put = requests.put(value["uploadUrl"], data=self._fetch_bytes(item.url), timeout=60)
        if not put.ok:
            raise InvalidMedia("LinkedIn rejected the image upload.", raw_detail=put.text[:500])
        return value["image"]

    def _upload_video(self, author: str, token: str, item: MediaRef) -> str:
        content = self._fetch_bytes(item.url)
        init = requests.post(
            f"{_API_BASE}/rest/videos?action=initializeUpload",
            headers=self._headers(token),
            json={
                "initializeUploadRequest": {
                    "owner": author,
                    "fileSizeBytes": len(content),
                    "uploadCaptions": False,
                    "uploadThumbnail": False,
                }
            },
            timeout=30,
        )
        if not init.ok:
            self._raise_error(init, action="initializing a LinkedIn video upload")
        value = init.json()["value"]

        part_ids = []
        for instruction in value["uploadInstructions"]:
            chunk = content[instruction["firstByte"] : instruction["lastByte"] + 1]
            put = requests.put(instruction["uploadUrl"], data=chunk, timeout=60)
            if not put.ok:
                raise InvalidMedia("LinkedIn rejected part of the video upload.", raw_detail=put.text[:500])
            part_ids.append(put.headers.get("ETag", ""))

        finalize = requests.post(
            f"{_API_BASE}/rest/videos?action=finalizeUpload",
            headers=self._headers(token),
            json={"finalizeUploadRequest": {"video": value["video"], "uploadedPartIds": part_ids}},
            timeout=30,
        )
        if not finalize.ok:
            self._raise_error(finalize, action="finalizing a LinkedIn video upload")
        return value["video"]

    def _create_post(self, author: str, token: str, text: str, media_urn: str | None) -> PublishOutcome:
        body: dict = {
            "author": author,
            "commentary": text,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }
        if media_urn:
            body["content"] = {"media": {"id": media_urn}}

        response = requests.post(f"{_API_BASE}/rest/posts", headers=self._headers(token), json=body, timeout=30)
        if not response.ok:
            self._raise_error(response, action="publishing a LinkedIn post")

        post_urn = response.headers.get("x-restli-id", "")
        permalink = f"https://www.linkedin.com/feed/update/{post_urn}/" if post_urn else None
        return PublishOutcome(done=True, remote_id=post_urn, permalink=permalink)

    def _raise_error(self, response: requests.Response, *, action: str) -> None:
        if response.status_code == 401:
            raise AuthExpired("LinkedIn rejected this channel's credentials.", raw_detail=response.text[:500])
        if response.status_code == 429:
            raise RateLimited(f"LinkedIn rate-limited {action}.", raw_detail=response.text[:500], retry_after=60)
        if response.status_code == 422:
            raise PermanentError(f"LinkedIn rejected the request while {action}.", raw_detail=response.text[:500])
        raise TransientError(
            f"LinkedIn returned {response.status_code} while {action}.", raw_detail=response.text[:500]
        )
