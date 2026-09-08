"""Shared Facebook Login plumbing for Facebook Pages and Instagram — both
sit on the same Graph API, both OAuth through the same Facebook Login
dialog, and both need the same "exchange for a long-lived user token, then
list the Pages this user manages" discovery step before a channel can be
created. Threads uses a different OAuth server (graph.threads.net) despite
also being Meta, so it doesn't share this module.

Managed app: OmniPost operates one Facebook App (Meta Tech Provider
Verification + app review, tracked outside this repo — see the revamp
plan's "platform approvals" workstream), so `client_id`/`client_secret` come
from settings, not a per-workspace AppCredential.
"""

from __future__ import annotations

import os
from urllib.parse import quote

import requests

from ..errors import PermanentError, TransientError

GRAPH_VERSION = "v19.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"
_AUTHORIZE_BASE = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"


def app_id() -> str:
    value = os.environ.get("META_APP_ID")
    if not value:
        raise PermanentError("META_APP_ID is not configured on this server.")
    return value


def app_secret() -> str:
    value = os.environ.get("META_APP_SECRET")
    if not value:
        raise PermanentError("META_APP_SECRET is not configured on this server.")
    return value


def build_authorize_url(redirect_uri: str, state: str, scope: str) -> str:
    params = {
        "client_id": app_id(),
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scope,
        "response_type": "code",
    }
    query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
    return f"{_AUTHORIZE_BASE}?{query}"


def exchange_code_for_user_token(code: str, redirect_uri: str) -> str:
    response = requests.get(
        f"{GRAPH_BASE}/oauth/access_token",
        params={"client_id": app_id(), "client_secret": app_secret(), "redirect_uri": redirect_uri, "code": code},
        timeout=15,
    )
    if not response.ok:
        raise PermanentError("Facebook rejected the authorization code.", raw_detail=response.text[:500])
    return response.json()["access_token"]


def exchange_for_long_lived_token(short_lived_token: str) -> str:
    response = requests.get(
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id(),
            "client_secret": app_secret(),
            "fb_exchange_token": short_lived_token,
        },
        timeout=15,
    )
    if not response.ok:
        raise TransientError("Could not exchange for a long-lived Facebook token.", raw_detail=response.text[:500])
    return response.json()["access_token"]


def list_pages(user_token: str) -> list[dict]:
    """Every Page this user manages, with each Page's own access token
    (needed for publishing — the user token itself can't post to a Page)
    and, when connected, its linked Instagram Business Account id."""
    response = requests.get(
        f"{GRAPH_BASE}/me/accounts",
        params={"access_token": user_token, "fields": "id,name,access_token,instagram_business_account"},
        timeout=15,
    )
    if not response.ok:
        raise TransientError("Could not list Facebook Pages for this user.", raw_detail=response.text[:500])
    return response.json().get("data", [])


def raise_graph_error(response: requests.Response, *, action: str) -> None:
    from ..errors import AuthExpired, InvalidMedia, PolicyViolation, RateLimited

    body = _safe_json(response)
    err = body.get("error", {}) if isinstance(body, dict) else {}
    code = err.get("code")
    subcode = err.get("error_subcode")
    message = err.get("message", "")

    if response.status_code == 401 or code in (190,):
        raise AuthExpired("Meta rejected this channel's credentials.", raw_detail=response.text[:500])
    if code == 4 or code == 17 or code == 32:  # app/user rate limits
        raise RateLimited(f"Meta rate-limited {action}.", raw_detail=response.text[:500], retry_after=60)
    if code == 2207026 or subcode in (2207001, 2207003):
        raise InvalidMedia(f"Meta rejected the media for {action}.", raw_detail=response.text[:500])
    if code == 368 or "policy" in message.lower():
        raise PolicyViolation(f"Meta blocked {action} for a policy violation.", raw_detail=response.text[:500])
    raise TransientError(f"Meta returned {response.status_code} while {action}.", raw_detail=response.text[:500])


def _safe_json(response: requests.Response) -> dict:
    try:
        return response.json()
    except ValueError:
        return {}
