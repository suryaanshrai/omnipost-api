import responses

from connectors.base import ChannelCredentials, MediaRef, PublishContext, PublishState
from connectors.platforms.youtube import YouTubeConnector

_API = "https://www.googleapis.com/youtube/v3"
_UPLOAD_INIT = "https://www.googleapis.com/upload/youtube/v3/videos"


def _ctx():
    return PublishContext(
        text="a short video",
        media=[MediaRef(url="https://cdn.example.com/clip.mp4", kind="video", mime_type="video/mp4")],
        post_kind="short_video",
        credentials=ChannelCredentials(values={"ACCESS_TOKEN": "tok"}),
        idempotency_key="k",
    )


def test_authorize_url_uses_managed_app_and_requests_offline_access(youtube_app_env):
    connector = YouTubeConnector()
    auth = connector.authorize_url(redirect_uri="https://omnipost.app/callback", state="csrf")
    assert "client_id=youtube-client-id" in auth.authorize_url
    assert "access_type=offline" in auth.authorize_url


@responses.activate
def test_exchange_code_fetches_channel_id(youtube_app_env):
    responses.add(
        responses.POST,
        "https://oauth2.googleapis.com/token",
        json={"access_token": "atok", "refresh_token": "rtok"},
        status=200,
    )
    responses.add(responses.GET, f"{_API}/channels", json={"items": [{"id": "channel1"}]}, status=200)
    connector = YouTubeConnector()
    creds = connector.exchange_code(code="c", redirect_uri="https://x/callback")
    assert creds.values == {"ACCESS_TOKEN": "atok", "REFRESH_TOKEN": "rtok"}
    assert creds.external_account_id == "channel1"


@responses.activate
def test_publish_initiates_resumable_upload_then_puts_bytes():
    responses.add(responses.GET, "https://cdn.example.com/clip.mp4", body=b"video-bytes", status=200)
    responses.add(
        responses.POST,
        f"{_UPLOAD_INIT}?uploadType=resumable&part=snippet,status",
        json={},
        status=200,
        headers={"Location": "https://upload.example.com/session/abc"},
    )
    responses.add(responses.PUT, "https://upload.example.com/session/abc", json={"id": "video1"}, status=200)

    connector = YouTubeConnector()
    outcome = connector.publish(_ctx(), PublishState())
    assert outcome.done is False
    assert outcome.state.step == "processing"
    assert outcome.state.data == {"video_id": "video1"}


@responses.activate
def test_poll_completes_when_processed():
    connector = YouTubeConnector()
    state = PublishState(step="processing", data={"video_id": "video1"})
    responses.add(
        responses.GET,
        f"{_API}/videos",
        json={"items": [{"status": {"uploadStatus": "processed"}}]},
        status=200,
    )
    outcome = connector.publish(_ctx(), state)
    assert outcome.done is True
    assert outcome.remote_id == "video1"
    assert outcome.permalink == "https://www.youtube.com/shorts/video1"


@responses.activate
def test_poll_raises_permanent_error_on_rejection():
    from connectors.errors import PermanentError

    connector = YouTubeConnector()
    state = PublishState(step="processing", data={"video_id": "video1"})
    responses.add(
        responses.GET,
        f"{_API}/videos",
        json={"items": [{"status": {"uploadStatus": "rejected"}}]},
        status=200,
    )
    try:
        connector.publish(_ctx(), state)
        raise AssertionError("expected PermanentError")
    except PermanentError:
        pass
