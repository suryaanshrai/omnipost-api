"""Mastodon OAuth. Publishing itself is still handled by the declarative
spec (specs/mastodon.yaml) — this class only adds the OAuth methods
DeclarativeConnector doesn't implement, then registers itself under the same
slug so the registry's "hand-written always wins" rule picks this up instead
of a bare DeclarativeConnector.

Mastodon has no fixed OAuth app: every instance (mastodon.social,
fosstodon.org, a private one) is a separate server that issues its own
client_id/client_secret via dynamic app registration
(POST /api/v1/apps — no auth required, no approval process). That
registration has to happen before an authorize URL can even be built, and
its result has to survive the redirect round-trip to the callback without a
database row — django.core.signing gives a tamper-proof, time-limited way to
carry it in the OAuth `state` parameter instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml
from django.core import signing

from ..base import ChannelCredentials, OAuthAuthorization, PublishContext, PublishOutcome, PublishState
from ..declarative import DeclarativeConnector
from ..errors import AuthExpired, PermanentError, RateLimited, TransientError

_SPEC_PATH = Path(__file__).parent.parent / "specs" / "mastodon.yaml"
_SIGNING_SALT = "connectors.mastodon.oauth"
_SIGNING_MAX_AGE_S = 600  # the whole authorize -> callback round trip must complete in 10 minutes
_SCOPES = "read:accounts write:statuses write:media"


class MastodonConnector(DeclarativeConnector):
    def __init__(self) -> None:
        with open(_SPEC_PATH, encoding="utf-8") as f:
            spec = yaml.safe_load(f)
        super().__init__(spec=spec)

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        if not ctx.parts:
            return super().publish(ctx, state)
        return self._publish_thread(ctx, state)

    def _publish_thread(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        """Posts ctx.parts as a reply chain (a Mastodon "thread"), pausing
        between parts for each part's delay_after_s via retry_after_s rather
        than blocking a worker — the same yield-and-re-enqueue pattern every
        other multi-step connector uses (see jobs/publisher.py)."""
        assert ctx.parts is not None
        parts = ctx.parts
        index = state.data.get("part_index", 0)
        remote_ids: list[str] = list(state.data.get("remote_ids", []))
        in_reply_to = remote_ids[-1] if remote_ids else None
        part = parts[index]

        payload: dict[str, str] = {"status": part.text}
        if in_reply_to:
            payload["in_reply_to_id"] = in_reply_to

        response = requests.post(
            f"https://{ctx.credentials['INSTANCE_DOMAIN']}/api/v1/statuses",
            headers={"Authorization": f"Bearer {ctx.credentials['ACCESS_TOKEN']}"},
            json=payload,
            timeout=30,
        )
        if not response.ok:
            self._raise_thread_error(response)

        body = response.json()
        remote_ids.append(str(body["id"]))
        next_index = index + 1

        if next_index >= len(parts):
            return PublishOutcome(done=True, remote_id=remote_ids[0], permalink=body.get("url"))

        return PublishOutcome(
            done=False,
            state=PublishState(step="thread_part", data={"part_index": next_index, "remote_ids": remote_ids}),
            retry_after_s=max(part.delay_after_s, 1),
        )

    def _raise_thread_error(self, response) -> None:
        if response.status_code == 429:
            header = response.headers.get("Retry-After")
            retry_after = float(header) if header and header.isdigit() else 30.0
            raise RateLimited(
                "Mastodon rate-limited this thread.", raw_detail=response.text[:500], retry_after=retry_after
            )
        if response.status_code == 401:
            raise AuthExpired("Mastodon rejected this channel's credentials.", raw_detail=response.text[:500])
        if response.status_code == 422:
            raise PermanentError("Mastodon rejected one part of this thread.", raw_detail=response.text[:500])
        raise TransientError(
            f"Mastodon returned {response.status_code} while posting a thread part.", raw_detail=response.text[:500]
        )

    def _register_app(self, instance_domain: str, redirect_uri: str) -> dict:
        response = requests.post(
            f"https://{instance_domain}/api/v1/apps",
            data={
                "client_name": "OmniPost",
                "redirect_uris": redirect_uri,
                "scopes": _SCOPES,
                "website": "https://omnipost.app",
            },
            timeout=15,
        )
        if not response.ok:
            raise TransientError(
                f"Could not register an app with {instance_domain}.", raw_detail=response.text[:500]
            )
        return response.json()

    def authorize_url(self, redirect_uri: str, state: str, **extra: str) -> OAuthAuthorization:
        instance_domain = extra.get("instance_domain")
        if not instance_domain:
            raise PermanentError("Mastodon requires 'instance_domain' to start OAuth.")

        app = self._register_app(instance_domain, redirect_uri)

        # Carries {instance_domain, client_id, client_secret} through the
        # redirect without a database row. Signed and short-lived, so a
        # tampered or replayed state param is rejected at exchange time.
        signed_state = signing.dumps(
            {
                "caller_state": state,
                "instance_domain": instance_domain,
                "client_id": app["client_id"],
                "client_secret": app["client_secret"],
            },
            salt=_SIGNING_SALT,
        )

        params = {
            "client_id": app["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _SCOPES,
            "state": signed_state,
        }
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return OAuthAuthorization(
            authorize_url=f"https://{instance_domain}/oauth/authorize?{query}", state=signed_state
        )

    def exchange_code(self, code: str, redirect_uri: str, **extra: str) -> ChannelCredentials:
        signed_state = extra.get("state", "")
        try:
            payload = signing.loads(signed_state, salt=_SIGNING_SALT, max_age=_SIGNING_MAX_AGE_S)
        except signing.BadSignature as exc:
            raise PermanentError("This authorization link is invalid or has expired.") from exc

        response = requests.post(
            f"https://{payload['instance_domain']}/oauth/token",
            data={
                "client_id": payload["client_id"],
                "client_secret": payload["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code": code,
                "scope": _SCOPES,
            },
            timeout=15,
        )
        if not response.ok:
            raise PermanentError("Mastodon rejected the authorization code.", raw_detail=response.text[:500])

        token_data = response.json()
        return ChannelCredentials(
            values={
                "ACCESS_TOKEN": token_data["access_token"],
                "INSTANCE_DOMAIN": payload["instance_domain"],
            }
        )

    def unwrap_caller_state(self, provider_state: str) -> str:
        try:
            payload = signing.loads(provider_state, salt=_SIGNING_SALT, max_age=_SIGNING_MAX_AGE_S)
        except signing.BadSignature as exc:
            raise PermanentError("This authorization link is invalid or has expired.") from exc
        return payload["caller_state"]

    def refresh(self, credentials: ChannelCredentials) -> ChannelCredentials:
        # Mastodon access tokens don't expire (they're revoked, not rotated),
        # so there's nothing to refresh — the token is valid until the user
        # revokes it, which shows up as an AUTH_EXPIRED on the next publish.
        return credentials

    def fetch_metrics(self, credentials: ChannelCredentials, remote_id: str) -> dict[str, Any]:
        response = requests.get(
            f"https://{credentials['INSTANCE_DOMAIN']}/api/v1/statuses/{remote_id}",
            headers={"Authorization": f"Bearer {credentials['ACCESS_TOKEN']}"},
            timeout=15,
        )
        if response.status_code == 401:
            raise AuthExpired("Mastodon rejected this channel's credentials.", raw_detail=response.text[:500])
        if not response.ok:
            return {}  # e.g. the status was deleted — nothing to report, not an error

        body = response.json()
        return {
            "likes": body.get("favourites_count"),
            "shares": body.get("reblogs_count"),
            "comments": body.get("replies_count"),
            "impressions": None,  # Mastodon doesn't expose view/impression counts
        }
