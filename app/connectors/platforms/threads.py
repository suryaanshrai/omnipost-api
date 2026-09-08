"""Threads (Meta), via the Threads API. Despite being a Meta product this is
a separate OAuth server and API host from Facebook/Instagram's Graph API
(threads.net / graph.threads.net, not facebook.com/graph.facebook.com), so
it doesn't share meta_oauth.py.

Publishing is the same create-container-then-publish two-step protocol as
Instagram (see instagram.py's docstring), with a poll in between for
video-backed containers. Threads natively supports reply chains, so — like
Mastodon (platforms/mastodon.py) — ctx.parts posts as a chain of replies via
`reply_to_id`, using the same yield-and-re-enqueue pattern for the delay
between parts.

Managed app: OmniPost operates one Threads API app (configured in the same
Meta developer console as the Facebook app, but with its own app id/secret
for this product) — THREADS_APP_ID / THREADS_APP_SECRET in the environment.
"""

from __future__ import annotations

import os
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
from ..errors import AuthExpired, PermanentError, RateLimited, TransientError

_AUTHORIZE_BASE = "https://www.threads.net/oauth/authorize"
_GRAPH_HOST = "https://graph.threads.net"
_GRAPH_BASE = f"{_GRAPH_HOST}/v1.0"
_SCOPES = "threads_basic,threads_content_publish"


def _app_id() -> str:
    value = os.environ.get("THREADS_APP_ID")
    if not value:
        raise PermanentError("THREADS_APP_ID is not configured on this server.")
    return value


def _app_secret() -> str:
    value = os.environ.get("THREADS_APP_SECRET")
    if not value:
        raise PermanentError("THREADS_APP_SECRET is not configured on this server.")
    return value


class ThreadsConnector(Connector):
    slug = "threads"
    capabilities = Capabilities(
        slug="threads",
        display_name="Threads",
        max_text_length=500,
        supports_threads=True,
        post_kinds=("text", "image", "video"),
        media=(
            MediaRule(kinds=("image",), max_count=10, max_size_mb=8),
            MediaRule(kinds=("video",), max_count=1, max_size_mb=1024, max_duration_s=300),
        ),
    )

    # --- OAuth ---

    def authorize_url(self, redirect_uri: str, state: str, **extra: str) -> OAuthAuthorization:
        params = {
            "client_id": _app_id(),
            "redirect_uri": redirect_uri,
            "scope": _SCOPES,
            "response_type": "code",
            "state": state,
        }
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return OAuthAuthorization(authorize_url=f"{_AUTHORIZE_BASE}?{query}", state=state)

    def exchange_code(self, code: str, redirect_uri: str, **extra: str) -> ChannelCredentials:
        response = requests.post(
            f"{_GRAPH_BASE}/oauth/access_token",
            data={
                "client_id": _app_id(),
                "client_secret": _app_secret(),
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code,
            },
            timeout=15,
        )
        if not response.ok:
            raise PermanentError("Threads rejected the authorization code.", raw_detail=response.text[:500])
        short = response.json()

        long_lived = requests.get(
            f"{_GRAPH_HOST}/access_token",
            params={
                "grant_type": "th_exchange_token",
                "client_secret": _app_secret(),
                "access_token": short["access_token"],
            },
            timeout=15,
        )
        access_token = long_lived.json()["access_token"] if long_lived.ok else short["access_token"]

        return ChannelCredentials(
            values={"ACCESS_TOKEN": access_token, "THREADS_USER_ID": str(short["user_id"])},
            external_account_id=str(short["user_id"]),
        )

    def refresh(self, credentials: ChannelCredentials) -> ChannelCredentials:
        response = requests.get(
            f"{_GRAPH_HOST}/refresh_access_token",
            params={"grant_type": "th_refresh_token", "access_token": credentials["ACCESS_TOKEN"]},
            timeout=15,
        )
        if not response.ok:
            raise AuthExpired("Threads rejected this channel's refresh attempt.", raw_detail=response.text[:500])
        return ChannelCredentials(
            values={**credentials.values, "ACCESS_TOKEN": response.json()["access_token"]},
            external_account_id=credentials.external_account_id,
        )

    # --- Publish ---

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        if ctx.parts:
            return self._publish_thread(ctx, state)
        if state.step == "start":
            return self._start(ctx, state)
        if state.step == "processing":
            return self._poll(ctx, state)
        raise PermanentError(f"Unknown Threads publish state '{state.step}'.")

    def _start(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        user_id = ctx.credentials["THREADS_USER_ID"]
        token = ctx.credentials["ACCESS_TOKEN"]
        creation_id = self._create_container(user_id, token, text=ctx.text, media=ctx.media[:1])

        if ctx.media and ctx.media[0].kind == "video":
            return PublishOutcome(
                done=False,
                state=PublishState(step="processing", data={"creation_id": creation_id}),
                retry_after_s=5,
            )
        return self._publish_container(user_id, token, creation_id)

    def _poll(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        user_id = ctx.credentials["THREADS_USER_ID"]
        token = ctx.credentials["ACCESS_TOKEN"]
        creation_id = state.data["creation_id"]

        response = requests.get(
            f"{_GRAPH_BASE}/{creation_id}", params={"fields": "status", "access_token": token}, timeout=15
        )
        if not response.ok:
            self._raise_error(response, action="checking Threads media status")
        status = response.json().get("status", "IN_PROGRESS")
        if status == "FINISHED":
            return self._publish_container(user_id, token, creation_id)
        if status == "ERROR":
            raise PermanentError("Threads failed to process this video.")
        return PublishOutcome(done=False, state=state, retry_after_s=5)

    def _publish_thread(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        assert ctx.parts is not None
        user_id = ctx.credentials["THREADS_USER_ID"]
        token = ctx.credentials["ACCESS_TOKEN"]
        parts = ctx.parts
        index = state.data.get("part_index", 0)
        remote_ids: list[str] = list(state.data.get("remote_ids", []))
        reply_to = remote_ids[-1] if remote_ids else None
        part = parts[index]

        creation_id = self._create_container(
            user_id, token, text=part.text, media=part.media[:1], reply_to_id=reply_to
        )
        published = self._publish_container(user_id, token, creation_id)
        remote_ids.append(published.remote_id or "")
        next_index = index + 1

        if next_index >= len(parts):
            return PublishOutcome(done=True, remote_id=remote_ids[0], permalink=published.permalink)
        return PublishOutcome(
            done=False,
            state=PublishState(step="thread_part", data={"part_index": next_index, "remote_ids": remote_ids}),
            retry_after_s=max(part.delay_after_s, 1),
        )

    def _create_container(
        self, user_id: str, token: str, *, text: str, media: list[MediaRef], reply_to_id: str | None = None
    ) -> str:
        payload: dict[str, str] = {"text": text, "access_token": token}
        if reply_to_id:
            payload["reply_to_id"] = reply_to_id
        if media:
            item = media[0]
            if item.kind == "video":
                payload["media_type"] = "VIDEO"
                payload["video_url"] = item.url
            else:
                payload["media_type"] = "IMAGE"
                payload["image_url"] = item.url
        else:
            payload["media_type"] = "TEXT"

        response = requests.post(f"{_GRAPH_BASE}/{user_id}/threads", data=payload, timeout=60)
        if not response.ok:
            self._raise_error(response, action="creating a Threads post")
        return response.json()["id"]

    def _publish_container(self, user_id: str, token: str, creation_id: str) -> PublishOutcome:
        response = requests.post(
            f"{_GRAPH_BASE}/{user_id}/threads_publish",
            data={"creation_id": creation_id, "access_token": token},
            timeout=30,
        )
        if not response.ok:
            self._raise_error(response, action="publishing a Threads post")
        media_id = response.json()["id"]
        return PublishOutcome(done=True, remote_id=media_id, permalink=f"https://www.threads.net/post/{media_id}")

    def _raise_error(self, response: requests.Response, *, action: str) -> None:
        if response.status_code == 401:
            raise AuthExpired("Threads rejected this channel's credentials.", raw_detail=response.text[:500])
        if response.status_code == 429:
            raise RateLimited(f"Threads rate-limited {action}.", raw_detail=response.text[:500], retry_after=60)
        raise TransientError(f"Threads returned {response.status_code} while {action}.", raw_detail=response.text[:500])
