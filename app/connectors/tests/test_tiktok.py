import responses

from connectors.base import ChannelCredentials, MediaRef, PublishContext, PublishState
from connectors.platforms.tiktok import TikTokConnector

_API = "https://open.tiktokapis.com/v2"


def _ctx():
    return PublishContext(
        text="check this out",
        media=[MediaRef(url="https://cdn.example.com/clip.mp4", kind="video", mime_type="video/mp4")],
        post_kind="short_video",
        credentials=ChannelCredentials(values={"ACCESS_TOKEN": "tok"}),
        idempotency_key="k",
    )


def test_authorize_url_uses_managed_app(tiktok_app_env):
    connector = TikTokConnector()
    auth = connector.authorize_url(redirect_uri="https://omnipost.app/callback", state="csrf")
    assert "client_key=tiktok-client-key" in auth.authorize_url


@responses.activate
def test_exchange_code(tiktok_app_env):
    responses.add(
        responses.POST,
        f"{_API}/oauth/token/",
        json={"access_token": "atok", "refresh_token": "rtok", "open_id": "openid1"},
        status=200,
    )
    connector = TikTokConnector()
    creds = connector.exchange_code(code="c", redirect_uri="https://x/callback")
    assert creds.values == {"ACCESS_TOKEN": "atok", "REFRESH_TOKEN": "rtok"}
    assert creds.external_account_id == "openid1"


@responses.activate
def test_publish_defaults_to_self_only_privacy_and_pull_from_url():
    responses.add(
        responses.POST, f"{_API}/post/publish/video/init/", json={"data": {"publish_id": "pub1"}}, status=200
    )
    connector = TikTokConnector()
    outcome = connector.publish(_ctx(), PublishState())
    assert outcome.done is False
    assert outcome.state.data == {"publish_id": "pub1"}

    import json

    sent = json.loads(responses.calls[0].request.body)
    assert sent["post_info"]["privacy_level"] == "SELF_ONLY"
    assert sent["source_info"] == {"source": "PULL_FROM_URL", "video_url": "https://cdn.example.com/clip.mp4"}


@responses.activate
def test_publish_requires_media():
    from connectors.errors import PermanentError

    connector = TikTokConnector()
    ctx = PublishContext(
        text="no video",
        media=[],
        post_kind="short_video",
        credentials=ChannelCredentials(values={"ACCESS_TOKEN": "tok"}),
        idempotency_key="k",
    )
    try:
        connector.publish(ctx, PublishState())
        raise AssertionError("expected PermanentError")
    except PermanentError:
        pass


@responses.activate
def test_poll_completes_when_publish_complete():
    connector = TikTokConnector()
    state = PublishState(step="processing", data={"publish_id": "pub1"})
    responses.add(
        responses.POST,
        f"{_API}/post/publish/status/fetch/",
        json={"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["vid999"]}},
        status=200,
    )
    outcome = connector.publish(_ctx(), state)
    assert outcome.done is True
    assert outcome.remote_id == "vid999"


@responses.activate
def test_poll_raises_permanent_error_on_failure():
    from connectors.errors import PermanentError

    connector = TikTokConnector()
    state = PublishState(step="processing", data={"publish_id": "pub1"})
    responses.add(
        responses.POST,
        f"{_API}/post/publish/status/fetch/",
        json={"data": {"status": "FAILED", "fail_reason": "video_too_long"}},
        status=200,
    )
    try:
        connector.publish(_ctx(), state)
        raise AssertionError("expected PermanentError")
    except PermanentError:
        pass
