import pytest
import responses
from django.core import signing

from connectors.errors import PermanentError
from connectors.platforms.mastodon import MastodonConnector


@responses.activate
def test_authorize_url_registers_app_and_signs_state():
    responses.add(
        responses.POST,
        "https://mastodon.social/api/v1/apps",
        json={"client_id": "cid123", "client_secret": "csecret456"},
        status=200,
    )
    connector = MastodonConnector()
    auth = connector.authorize_url(
        redirect_uri="https://omnipost.app/callback", state="csrf-token", instance_domain="mastodon.social"
    )

    assert "mastodon.social/oauth/authorize" in auth.authorize_url
    assert "client_id=cid123" in auth.authorize_url

    payload = signing.loads(auth.state, salt="connectors.mastodon.oauth")
    assert payload["client_id"] == "cid123"
    assert payload["client_secret"] == "csecret456"
    assert payload["instance_domain"] == "mastodon.social"
    assert payload["caller_state"] == "csrf-token"


def test_authorize_url_requires_instance_domain():
    connector = MastodonConnector()
    with pytest.raises(PermanentError):
        connector.authorize_url(redirect_uri="https://x/callback", state="s")


@responses.activate
def test_exchange_code_round_trip():
    signed_state = signing.dumps(
        {
            "caller_state": "csrf-token",
            "instance_domain": "mastodon.social",
            "client_id": "cid123",
            "client_secret": "csecret456",
        },
        salt="connectors.mastodon.oauth",
    )
    responses.add(
        responses.POST,
        "https://mastodon.social/oauth/token",
        json={"access_token": "user-token-abc", "token_type": "Bearer"},
        status=200,
    )
    connector = MastodonConnector()
    creds = connector.exchange_code(code="auth-code-xyz", redirect_uri="https://x/callback", state=signed_state)

    assert creds.values["ACCESS_TOKEN"] == "user-token-abc"
    assert creds.values["INSTANCE_DOMAIN"] == "mastodon.social"

    sent_body = responses.calls[0].request.body
    assert "code=auth-code-xyz" in sent_body
    assert "client_secret=csecret456" in sent_body


def test_exchange_code_rejects_tampered_state():
    connector = MastodonConnector()
    with pytest.raises(PermanentError):
        connector.exchange_code(code="c", redirect_uri="https://x/callback", state="not-a-valid-signature")


def test_exchange_code_rejects_state_signed_with_different_salt():
    # Simulates a state token forged/reused from a different signing context.
    wrong_salt_state = signing.dumps({"instance_domain": "evil.example"}, salt="some.other.salt")
    connector = MastodonConnector()
    with pytest.raises(PermanentError):
        connector.exchange_code(code="c", redirect_uri="https://x/callback", state=wrong_salt_state)


@responses.activate
def test_mastodon_connector_still_publishes_via_declarative_spec():
    responses.add(
        responses.POST,
        "https://mastodon.social/api/v1/statuses",
        json={"id": "1", "url": "https://mastodon.social/@me/1"},
        status=200,
    )
    from connectors.base import ChannelCredentials, PublishContext, PublishState

    connector = MastodonConnector()
    ctx = PublishContext(
        text="hi",
        media=[],
        post_kind="text",
        credentials=ChannelCredentials(values={"ACCESS_TOKEN": "t", "INSTANCE_DOMAIN": "mastodon.social"}),
        idempotency_key="k",
    )
    outcome = connector.publish(ctx, PublishState())
    assert outcome.done is True
