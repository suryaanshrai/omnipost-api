"""Orchestrates the OAuth connect flow across any connector: signs the
service's own bookkeeping (workspace, connector slug, display name) into the
`state` a connector's authorize_url embeds, and unpacks it again on the
callback via Connector.unwrap_caller_state — see that method's docstring for
why the round trip needs two independently-signed layers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from connectors.registry import get as get_connector
from django.core import signing

from ..models import AppCredential, Channel

_SIGNING_SALT = "omnipost.oauth.session"
_SIGNING_MAX_AGE_S = 600


class MissingAppCredential(Exception):
    """Raised when a `requires_own_app` connector (LinkedIn, X) has no
    AppCredential registered for the workspace yet. Caught by the OAuth views
    and turned into a 400 telling the user to register their own app first —
    never a generic 500, since this is an expected, actionable state."""


@dataclass(frozen=True)
class OAuthStart:
    authorize_url: str
    provider_state: str  # what the caller must have the browser send back as `state`


def _own_app_extra(workspace_id: int, connector_slug: str) -> dict[str, str]:
    try:
        credential = AppCredential.objects.get(workspace_id=workspace_id, connector_slug=connector_slug)
    except AppCredential.DoesNotExist:
        raise MissingAppCredential(
            f"Register your own {connector_slug} app (client id/secret) for this workspace before connecting "
            "a channel — see /app-credentials/."
        ) from None
    return {"client_id": credential.client_id, "client_secret": credential.get_client_secret()}


def start(
    *,
    workspace_id: int,
    connector_slug: str,
    display_name: str,
    redirect_uri: str,
    extra: dict[str, str],
    user_id: int | None = None,
) -> OAuthStart:
    connector = get_connector(connector_slug)
    merged_extra = dict(extra)
    if connector.requires_own_app:
        merged_extra.update(_own_app_extra(workspace_id, connector_slug))
    caller_state = signing.dumps(
        {
            "workspace_id": workspace_id,
            "connector_slug": connector_slug,
            "display_name": display_name,
            "user_id": user_id,
        },
        salt=_SIGNING_SALT,
    )
    authorization = connector.authorize_url(redirect_uri=redirect_uri, state=caller_state, **merged_extra)
    return OAuthStart(authorize_url=authorization.authorize_url, provider_state=authorization.state)


class StateMismatch(Exception):
    """The callback doesn't match the flow that was started: another user's
    state, or a different connector than the one that signed it."""


def complete(
    *,
    connector_slug: str,
    code: str,
    redirect_uri: str,
    provider_state: str,
    authorize: Callable[[int], None] | None = None,
    user_id: int | None = None,
) -> Channel:
    """Finishes an OAuth connect. `authorize(workspace_id)` runs before the
    code is exchanged or anything is written, so a caller without access
    never creates (even briefly) a channel holding real credentials.

    Reconnecting an account that's already a channel in this workspace
    refreshes that channel in place instead of creating a duplicate, so its
    scheduled posts keep working."""
    connector = get_connector(connector_slug)
    caller_state = connector.unwrap_caller_state(provider_state)
    session = signing.loads(caller_state, salt=_SIGNING_SALT, max_age=_SIGNING_MAX_AGE_S)
    if session["connector_slug"] != connector_slug:
        raise StateMismatch("This sign-in was started for a different platform.")
    if session.get("user_id") is not None and user_id is not None and session["user_id"] != user_id:
        raise StateMismatch("This sign-in was started by a different account.")
    workspace_id = session["workspace_id"]
    if authorize is not None:
        authorize(workspace_id)

    exchange_extra = {"state": provider_state}
    if connector.requires_own_app:
        exchange_extra.update(_own_app_extra(workspace_id, connector_slug))

    credentials = connector.exchange_code(code=code, redirect_uri=redirect_uri, **exchange_extra)
    external_id = credentials.external_account_id or ""

    channel = None
    if external_id:
        channel = Channel.objects.filter(
            workspace_id=workspace_id, connector_slug=connector_slug, external_account_id=external_id
        ).first()
    if channel is None:
        from .billing import check_channel_limit

        check_channel_limit(workspace_id)
        channel = Channel.objects.create(
            workspace_id=workspace_id,
            connector_slug=connector_slug,
            display_name=session["display_name"],
            external_account_id=external_id,
        )
    else:
        channel.health = Channel.HEALTH_HEALTHY
        channel.health_detail = ""
    channel.set_credentials(credentials.values)
    channel.token_expires_at = connector.token_expiry(credentials)
    channel.save()
    return channel
