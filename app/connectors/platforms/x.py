"""X (formerly Twitter): tweets via API v2 (api.twitter.com/2/tweets), media
via the older chunked v1.1 upload endpoint (upload.twitter.com), which v2
still requires for anything with an image/gif/video attached.

`requires_own_app = True`: X charges the *app* per post (and more per post
with a link) — a single OmniPost-managed app would mean OmniPost eating an
unbounded, usage-scaling bill for every customer's every tweet. Each
workspace registers its own X app via AppCredential; OAuth is 2.0 with PKCE,
which is why `authorize_url` has to smuggle a `code_verifier` through the
redirect the same way Mastodon smuggles its per-instance client credentials
(platforms/mastodon.py) — via a signed `state` blob, since there's no
database row at that point in the flow yet.

Video/gif media processes asynchronously after FINALIZE; images don't. That
async wait uses the same yield-and-re-enqueue pattern as every other
multi-step connector, and threads (native to X) reuse it again between
parts — this connector is the union of every pattern the others use
individually: PKCE state-smuggling, media processing polls, and reply-chain
threading.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import quote

import requests
from django.core import signing

from ..base import (
    ChannelCredentials,
    Connector,
    ContentPart,
    MediaRef,
    OAuthAuthorization,
    PublishContext,
    PublishOutcome,
    PublishState,
)
from ..capabilities import Capabilities, MediaRule
from ..errors import AuthExpired, InvalidMedia, PermanentError, RateLimited, TransientError

_AUTHORIZE_URL = "https://twitter.com/i/oauth2/authorize"
_TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
_API_BASE = "https://api.twitter.com/2"
_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"
_SCOPES = "tweet.read tweet.write users.read offline.access"
_SIGNING_SALT = "connectors.x.oauth"
_SIGNING_MAX_AGE_S = 600
_CHUNK_SIZE = 4 * 1024 * 1024
_MEDIA_CATEGORY = {"image": "tweet_image", "gif": "tweet_gif", "video": "tweet_video"}


class XConnector(Connector):
    slug = "x"
    requires_own_app = True
    capabilities = Capabilities(
        slug="x",
        display_name="X",
        max_text_length=280,
        supports_threads=True,
        post_kinds=("text", "image", "video"),
        media=(
            MediaRule(kinds=("image",), max_count=4, max_size_mb=5),
            MediaRule(kinds=("gif",), max_count=1, max_size_mb=15),
            MediaRule(kinds=("video",), max_count=1, max_size_mb=512, max_duration_s=140, min_duration_s=1),
        ),
    )

    # --- OAuth (2.0, PKCE) ---

    def authorize_url(self, redirect_uri: str, state: str, **extra: str) -> OAuthAuthorization:
        client_id = extra.get("client_id")
        if not client_id:
            raise PermanentError("X requires this workspace's own app client_id.")

        code_verifier = secrets.token_urlsafe(64)[:128]
        code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().rstrip("=")
        signed_state = signing.dumps({"caller_state": state, "code_verifier": code_verifier}, salt=_SIGNING_SALT)

        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": _SCOPES,
            "state": signed_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return OAuthAuthorization(authorize_url=f"{_AUTHORIZE_URL}?{query}", state=signed_state)

    def unwrap_caller_state(self, provider_state: str) -> str:
        try:
            payload = signing.loads(provider_state, salt=_SIGNING_SALT, max_age=_SIGNING_MAX_AGE_S)
        except signing.BadSignature as exc:
            raise PermanentError("This authorization link is invalid or has expired.") from exc
        return payload["caller_state"]

    def exchange_code(self, code: str, redirect_uri: str, **extra: str) -> ChannelCredentials:
        client_id = extra.get("client_id")
        client_secret = extra.get("client_secret")
        if not client_id or not client_secret:
            raise PermanentError("X requires this workspace's own app client_id/client_secret.")
        signed_state = extra.get("state", "")
        try:
            payload = signing.loads(signed_state, salt=_SIGNING_SALT, max_age=_SIGNING_MAX_AGE_S)
        except signing.BadSignature as exc:
            raise PermanentError("This authorization link is invalid or has expired.") from exc

        response = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": payload["code_verifier"],
                "client_id": client_id,
            },
            auth=(client_id, client_secret),
            timeout=15,
        )
        if not response.ok:
            raise PermanentError("X rejected the authorization code.", raw_detail=response.text[:500])
        token_data = response.json()

        userinfo = requests.get(
            f"{_API_BASE}/users/me",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
            timeout=15,
        )
        user_id = userinfo.json().get("data", {}).get("id", "") if userinfo.ok else ""

        return ChannelCredentials(
            values={
                "ACCESS_TOKEN": token_data["access_token"],
                "REFRESH_TOKEN": token_data.get("refresh_token", ""),
                "CLIENT_ID": client_id,
                "CLIENT_SECRET": client_secret,
            },
            external_account_id=user_id,
        )

    def refresh(self, credentials: ChannelCredentials) -> ChannelCredentials:
        refresh_token = credentials.get("REFRESH_TOKEN")
        if not refresh_token:
            return credentials
        response = requests.post(
            _TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": credentials["CLIENT_ID"]},
            auth=(credentials["CLIENT_ID"], credentials["CLIENT_SECRET"]),
            timeout=15,
        )
        if not response.ok:
            raise AuthExpired("X rejected this channel's refresh token.", raw_detail=response.text[:500])
        token_data = response.json()
        return ChannelCredentials(
            values={
                **credentials.values,
                "ACCESS_TOKEN": token_data["access_token"],
                "REFRESH_TOKEN": token_data.get("refresh_token", refresh_token),
            },
            external_account_id=credentials.external_account_id,
        )

    # --- Publish ---

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        if ctx.parts:
            return self._publish_thread(ctx, state)
        if state.step == "start":
            return self._start(ctx)
        if state.step == "media_processing":
            return self._continue_after_media(ctx, state)
        raise PermanentError(f"Unknown X publish state '{state.step}'.")

    def _start(self, ctx: PublishContext) -> PublishOutcome:
        token = ctx.credentials["ACCESS_TOKEN"]
        if not ctx.media:
            return self._done(self._create_tweet(token, ctx.text))

        media_id, processing = self._upload_media(token, ctx.media[0])
        if self._still_processing(processing):
            return PublishOutcome(
                done=False,
                state=PublishState(step="media_processing", data={"media_id": media_id, "text": ctx.text}),
                retry_after_s=processing.get("check_after_secs", 5),
            )
        return self._done(self._create_tweet(token, ctx.text, media_id=media_id))

    def _continue_after_media(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        token = ctx.credentials["ACCESS_TOKEN"]
        media_id = state.data["media_id"]
        processing = self._check_media_status(token, media_id)
        proc_state = processing.get("state", "succeeded")
        if proc_state == "succeeded":
            return self._done(self._create_tweet(token, state.data["text"], media_id=media_id))
        if proc_state == "failed":
            raise PermanentError("X failed to process this media.")
        return PublishOutcome(done=False, state=state, retry_after_s=processing.get("check_after_secs", 5))

    def _publish_thread(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        assert ctx.parts is not None
        parts = ctx.parts
        if state.step == "thread_media_processing":
            return self._continue_thread_after_media(ctx, state, parts)
        part_index = state.data.get("part_index", 0)
        remote_ids: list[str] = list(state.data.get("remote_ids", []))
        return self._post_thread_part(ctx, parts, part_index, remote_ids)

    def _post_thread_part(
        self, ctx: PublishContext, parts: list[ContentPart], part_index: int, remote_ids: list[str]
    ) -> PublishOutcome:
        part = parts[part_index]
        token = ctx.credentials["ACCESS_TOKEN"]

        if part.media:
            media_id, processing = self._upload_media(token, part.media[0])
            if self._still_processing(processing):
                return PublishOutcome(
                    done=False,
                    state=PublishState(
                        step="thread_media_processing",
                        data={"media_id": media_id, "part_index": part_index, "remote_ids": remote_ids},
                    ),
                    retry_after_s=processing.get("check_after_secs", 5),
                )
            return self._finish_thread_part(ctx, parts, part_index, remote_ids, media_id)
        return self._finish_thread_part(ctx, parts, part_index, remote_ids, None)

    def _continue_thread_after_media(
        self, ctx: PublishContext, state: PublishState, parts: list[ContentPart]
    ) -> PublishOutcome:
        token = ctx.credentials["ACCESS_TOKEN"]
        media_id = state.data["media_id"]
        part_index = state.data["part_index"]
        remote_ids = state.data["remote_ids"]
        processing = self._check_media_status(token, media_id)
        proc_state = processing.get("state", "succeeded")
        if proc_state == "succeeded":
            return self._finish_thread_part(ctx, parts, part_index, remote_ids, media_id)
        if proc_state == "failed":
            raise PermanentError("X failed to process this media.")
        return PublishOutcome(done=False, state=state, retry_after_s=processing.get("check_after_secs", 5))

    def _finish_thread_part(
        self,
        ctx: PublishContext,
        parts: list[ContentPart],
        part_index: int,
        remote_ids: list[str],
        media_id: str | None,
    ) -> PublishOutcome:
        part = parts[part_index]
        token = ctx.credentials["ACCESS_TOKEN"]
        reply_to = remote_ids[-1] if remote_ids else None
        tweet_id = self._create_tweet(token, part.text, media_id=media_id, reply_to=reply_to)
        remote_ids = [*remote_ids, tweet_id]
        next_index = part_index + 1
        if next_index >= len(parts):
            return self._done(remote_ids[0])
        return PublishOutcome(
            done=False,
            state=PublishState(step="thread_part", data={"part_index": next_index, "remote_ids": remote_ids}),
            retry_after_s=max(part.delay_after_s, 1),
        )

    def _still_processing(self, processing: dict[str, Any] | None) -> bool:
        return processing is not None and processing.get("state") in ("pending", "in_progress")

    def _done(self, tweet_id: str) -> PublishOutcome:
        return PublishOutcome(done=True, remote_id=tweet_id, permalink=f"https://x.com/i/web/status/{tweet_id}")

    def _create_tweet(self, token: str, text: str, *, media_id: str | None = None, reply_to: str | None = None) -> str:
        body: dict = {"text": text}
        if media_id:
            body["media"] = {"media_ids": [media_id]}
        if reply_to:
            body["reply"] = {"in_reply_to_tweet_id": reply_to}
        response = requests.post(
            f"{_API_BASE}/tweets",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        if not response.ok:
            self._raise_error(response, action="posting a tweet")
        return response.json()["data"]["id"]

    def _upload_media(self, token: str, item: MediaRef) -> tuple[str, dict[str, Any]]:
        content = self._fetch_bytes(item.url)
        category = _MEDIA_CATEGORY.get(item.kind, "tweet_image")
        headers = {"Authorization": f"Bearer {token}"}

        init = requests.post(
            _UPLOAD_URL,
            headers=headers,
            data={
                "command": "INIT",
                "total_bytes": len(content),
                "media_type": item.mime_type,
                "media_category": category,
            },
            timeout=30,
        )
        if not init.ok:
            self._raise_error(init, action="initializing media upload")
        media_id = init.json()["media_id_string"]

        for index, offset in enumerate(range(0, len(content), _CHUNK_SIZE)):
            chunk = content[offset : offset + _CHUNK_SIZE]
            append = requests.post(
                _UPLOAD_URL,
                headers=headers,
                data={"command": "APPEND", "media_id": media_id, "segment_index": index},
                files={"media": chunk},
                timeout=60,
            )
            if not append.ok:
                self._raise_error(append, action="uploading media")

        finalize = requests.post(
            _UPLOAD_URL, headers=headers, data={"command": "FINALIZE", "media_id": media_id}, timeout=30
        )
        if not finalize.ok:
            self._raise_error(finalize, action="finalizing media upload")
        return media_id, finalize.json().get("processing_info", {})

    def _check_media_status(self, token: str, media_id: str) -> dict[str, Any]:
        response = requests.get(
            _UPLOAD_URL,
            headers={"Authorization": f"Bearer {token}"},
            params={"command": "STATUS", "media_id": media_id},
            timeout=15,
        )
        if not response.ok:
            self._raise_error(response, action="checking media processing status")
        return response.json().get("processing_info", {})

    def _fetch_bytes(self, url: str) -> bytes:
        fetched = requests.get(url, timeout=60)
        if not fetched.ok:
            raise TransientError(f"Could not fetch media from {url} for upload.")
        return fetched.content

    def _raise_error(self, response: requests.Response, *, action: str) -> None:
        if response.status_code == 401:
            raise AuthExpired("X rejected this channel's credentials.", raw_detail=response.text[:500])
        if response.status_code == 429:
            header = response.headers.get("x-rate-limit-reset")
            raise RateLimited(
                f"X rate-limited {action}.",
                raw_detail=response.text[:500],
                retry_after=float(header) if header else 60.0,
            )
        if response.status_code in (400, 403, 422):
            raise InvalidMedia(f"X rejected the request while {action}.", raw_detail=response.text[:500])
        raise TransientError(f"X returned {response.status_code} while {action}.", raw_detail=response.text[:500])
