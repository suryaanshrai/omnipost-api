import responses

from connectors.base import ChannelCredentials, MediaRef, PublishContext, PublishState
from connectors.platforms.linkedin import LinkedInConnector

_API = "https://api.linkedin.com"


def _creds():
    return ChannelCredentials(values={"ACCESS_TOKEN": "tok", "MEMBER_URN": "urn:li:person:abc"})


def _ctx(text="hi linkedin", media=None):
    return PublishContext(
        text=text, media=media or [], post_kind="image" if media else "text", credentials=_creds(), idempotency_key="k"
    )


def test_requires_own_app_flag_is_set():
    assert LinkedInConnector().requires_own_app is True


def test_authorize_url_requires_client_id():
    from connectors.errors import PermanentError

    connector = LinkedInConnector()
    try:
        connector.authorize_url(redirect_uri="https://x/callback", state="csrf")
        raise AssertionError("expected PermanentError")
    except PermanentError:
        pass


def test_authorize_url_uses_workspace_app_credential():
    connector = LinkedInConnector()
    auth = connector.authorize_url(
        redirect_uri="https://x/callback", state="csrf", client_id="workspace-client-id"
    )
    assert "client_id=workspace-client-id" in auth.authorize_url
    assert auth.state == "csrf"


@responses.activate
def test_exchange_code_fetches_member_urn():
    responses.add(
        responses.POST, "https://www.linkedin.com/oauth/v2/accessToken", json={"access_token": "tok"}, status=200
    )
    responses.add(responses.GET, "https://api.linkedin.com/v2/userinfo", json={"sub": "abc123"}, status=200)
    connector = LinkedInConnector()
    creds = connector.exchange_code(
        code="c", redirect_uri="https://x/callback", client_id="cid", client_secret="csecret"
    )
    assert creds.values == {"ACCESS_TOKEN": "tok", "MEMBER_URN": "urn:li:person:abc123"}
    assert creds.external_account_id == "abc123"


@responses.activate
def test_text_only_publish():
    responses.add(
        responses.POST,
        f"{_API}/rest/posts",
        json={},
        status=201,
        headers={"x-restli-id": "urn:li:share:999"},
    )
    connector = LinkedInConnector()
    outcome = connector.publish(_ctx(), PublishState())
    assert outcome.done is True
    assert outcome.remote_id == "urn:li:share:999"
    assert outcome.permalink == "https://www.linkedin.com/feed/update/urn:li:share:999/"


@responses.activate
def test_image_publish_uploads_then_posts():
    responses.add(
        responses.POST,
        f"{_API}/rest/images?action=initializeUpload",
        json={"value": {"uploadUrl": "https://upload.linkedin.com/put/abc", "image": "urn:li:image:xyz"}},
        status=200,
    )
    responses.add(responses.GET, "https://cdn.example.com/pic.jpg", body=b"bytes", status=200)
    responses.add(responses.PUT, "https://upload.linkedin.com/put/abc", status=201)
    responses.add(
        responses.POST, f"{_API}/rest/posts", json={}, status=201, headers={"x-restli-id": "urn:li:share:1000"}
    )

    connector = LinkedInConnector()
    media = [MediaRef(url="https://cdn.example.com/pic.jpg", kind="image", mime_type="image/jpeg")]
    outcome = connector.publish(_ctx(media=media), PublishState())

    assert outcome.done is True
    post_body = responses.calls[3].request.body
    assert b"urn:li:image:xyz" in post_body


@responses.activate
def test_video_publish_waits_for_processing():
    content = b"video-bytes"
    responses.add(
        responses.POST,
        f"{_API}/rest/videos?action=initializeUpload",
        json={
            "value": {
                "video": "urn:li:video:vvv",
                "uploadInstructions": [
                    {"uploadUrl": "https://upload.linkedin.com/v/1", "firstByte": 0, "lastByte": len(content) - 1}
                ],
            }
        },
        status=200,
    )
    responses.add(responses.GET, "https://cdn.example.com/clip.mp4", body=content, status=200)
    responses.add(responses.PUT, "https://upload.linkedin.com/v/1", status=201, headers={"ETag": "etag1"})
    responses.add(
        responses.POST,
        f"{_API}/rest/videos?action=finalizeUpload",
        json={},
        status=200,
    )

    connector = LinkedInConnector()
    media = [MediaRef(url="https://cdn.example.com/clip.mp4", kind="video", mime_type="video/mp4")]
    ctx = PublishContext(text="video post", media=media, post_kind="video", credentials=_creds(), idempotency_key="k")
    outcome = connector.publish(ctx, PublishState())

    assert outcome.done is False
    assert outcome.state.step == "processing"
    assert outcome.state.data["media_urn"] == "urn:li:video:vvv"

    responses.add(
        responses.GET, f"{_API}/rest/videos/urn%3Ali%3Avideo%3Avvv", json={"status": "AVAILABLE"}, status=200
    )
    responses.add(
        responses.POST, f"{_API}/rest/posts", json={}, status=201, headers={"x-restli-id": "urn:li:share:2000"}
    )
    done = connector.publish(ctx, outcome.state)
    assert done.done is True
    assert done.remote_id == "urn:li:share:2000"
