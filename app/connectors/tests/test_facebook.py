import responses

from connectors.base import ChannelCredentials, PublishContext, PublishState
from connectors.platforms.facebook import FacebookConnector

_GRAPH = "https://graph.facebook.com/v19.0"


def _ctx(text="hello page", link=None):
    return PublishContext(
        text=text,
        media=[],
        post_kind="text",
        credentials=ChannelCredentials(values={"PAGE_ACCESS_TOKEN": "page-token", "PAGE_ID": "page123"}),
        idempotency_key="k",
        link=link,
    )


def test_authorize_url_uses_managed_app(meta_app_env):
    connector = FacebookConnector()
    auth = connector.authorize_url(redirect_uri="https://omnipost.app/callback", state="csrf")
    assert "client_id=meta-app-id" in auth.authorize_url
    assert "www.facebook.com" in auth.authorize_url
    assert auth.state == "csrf"


@responses.activate
def test_exchange_code_discovers_page_and_wraps_credentials(meta_app_env):
    responses.add(responses.GET, f"{_GRAPH}/oauth/access_token", json={"access_token": "short"}, status=200)
    responses.add(responses.GET, f"{_GRAPH}/oauth/access_token", json={"access_token": "long-lived"}, status=200)
    responses.add(
        responses.GET,
        f"{_GRAPH}/me/accounts",
        json={"data": [{"id": "page123", "name": "My Page", "access_token": "page-token"}]},
        status=200,
    )
    connector = FacebookConnector()
    creds = connector.exchange_code(code="abc", redirect_uri="https://omnipost.app/callback")
    assert creds.values == {"PAGE_ACCESS_TOKEN": "page-token", "PAGE_ID": "page123"}
    assert creds.external_account_id == "page123"


@responses.activate
def test_publish_text_post_hits_feed_endpoint():
    responses.add(responses.POST, f"{_GRAPH}/page123/feed", json={"id": "page123_999"}, status=200)
    connector = FacebookConnector()
    outcome = connector.publish(_ctx(link="https://example.com"), PublishState())
    assert outcome.done is True
    assert outcome.remote_id == "page123_999"
    assert outcome.permalink == "https://www.facebook.com/page123_999"
    sent = responses.calls[0].request.body
    assert "link=https" in sent


@responses.activate
def test_publish_maps_expired_token_to_auth_expired():
    from connectors.errors import AuthExpired

    responses.add(
        responses.POST,
        f"{_GRAPH}/page123/feed",
        json={"error": {"code": 190, "message": "Error validating access token"}},
        status=401,
    )
    connector = FacebookConnector()
    try:
        connector.publish(_ctx(), PublishState())
        raise AssertionError("expected AuthExpired")
    except AuthExpired:
        pass
