"""Orchestrates the OAuth connect flow across any connector: signs the
service's own bookkeeping (workspace, connector slug, display name) into the
`state` a connector's authorize_url embeds, and unpacks it again on the
callback via Connector.unwrap_caller_state — see that method's docstring for
why the round trip needs two independently-signed layers.
"""

from __future__ import annotations

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
    *, workspace_id: int, connector_slug: str, display_name: str, redirect_uri: str, extra: dict[str, str]
) -> OAuthStart:
    connector = get_connector(connector_slug)
    merged_extra = dict(extra)
    if connector.requires_own_app:
        merged_extra.update(_own_app_extra(workspace_id, connector_slug))
    caller_state = signing.dumps(
        {"workspace_id": workspace_id, "connector_slug": connector_slug, "display_name": display_name},
        salt=_SIGNING_SALT,
    )
    authorization = connector.authorize_url(redirect_uri=redirect_uri, state=caller_state, **merged_extra)
    return OAuthStart(authorize_url=authorization.authorize_url, provider_state=authorization.state)


def complete(*, connector_slug: str, code: str, redirect_uri: str, provider_state: str) -> Channel:
    connector = get_connector(connector_slug)
    caller_state = connector.unwrap_caller_state(provider_state)
    session = signing.loads(caller_state, salt=_SIGNING_SALT, max_age=_SIGNING_MAX_AGE_S)

    exchange_extra = {"state": provider_state}
    if connector.requires_own_app:
        exchange_extra.update(_own_app_extra(session["workspace_id"], connector_slug))

    credentials = connector.exchange_code(code=code, redirect_uri=redirect_uri, **exchange_extra)

    channel = Channel.objects.create(
        workspace_id=session["workspace_id"],
        connector_slug=session["connector_slug"],
        display_name=session["display_name"],
        external_account_id=credentials.external_account_id or "",
    )
    channel.set_credentials(credentials.values)
    return channel
