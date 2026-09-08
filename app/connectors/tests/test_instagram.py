import responses

from connectors.base import ChannelCredentials, MediaRef, PublishContext, PublishState
from connectors.platforms.instagram import InstagramConnector

_GRAPH = "https://graph.facebook.com/v19.0"


def _ctx(post_kind, media):
    return PublishContext(
        text="caption",
        media=media,
        post_kind=post_kind,
        credentials=ChannelCredentials(values={"PAGE_ACCESS_TOKEN": "page-token", "IG_USER_ID": "ig123"}),
        idempotency_key="k",
    )


def _image(url="https://cdn.example.com/a.jpg"):
    return MediaRef(url=url, kind="image", mime_type="image/jpeg")


def _video(url="https://cdn.example.com/a.mp4"):
    return MediaRef(url=url, kind="video", mime_type="video/mp4")


@responses.activate
def test_exchange_code_requires_linked_instagram_account(meta_app_env):
    from connectors.errors import PermanentError

    responses.add(responses.GET, f"{_GRAPH}/oauth/access_token", json={"access_token": "short"}, status=200)
    responses.add(responses.GET, f"{_GRAPH}/oauth/access_token", json={"access_token": "long"}, status=200)
    responses.add(
        responses.GET, f"{_GRAPH}/me/accounts", json={"data": [{"id": "p1", "access_token": "pt"}]}, status=200
    )
    connector = InstagramConnector()
    try:
        connector.exchange_code(code="abc", redirect_uri="https://x/callback")
        raise AssertionError("expected PermanentError")
    except PermanentError:
        pass


@responses.activate
def test_exchange_code_picks_page_with_linked_ig_account(meta_app_env):
    responses.add(responses.GET, f"{_GRAPH}/oauth/access_token", json={"access_token": "short"}, status=200)
    responses.add(responses.GET, f"{_GRAPH}/oauth/access_token", json={"access_token": "long"}, status=200)
    responses.add(
        responses.GET,
        f"{_GRAPH}/me/accounts",
        json={
            "data": [
                {"id": "p1", "access_token": "pt1"},
                {"id": "p2", "access_token": "pt2", "instagram_business_account": {"id": "ig999"}},
            ]
        },
        status=200,
    )
    connector = InstagramConnector()
    creds = connector.exchange_code(code="abc", redirect_uri="https://x/callback")
    assert creds.values == {"PAGE_ACCESS_TOKEN": "pt2", "IG_USER_ID": "ig999"}
    assert creds.external_account_id == "ig999"


@responses.activate
def test_image_publish_skips_processing_poll():
    responses.add(responses.POST, f"{_GRAPH}/ig123/media", json={"id": "container1"}, status=200)
    responses.add(responses.POST, f"{_GRAPH}/ig123/media_publish", json={"id": "media1"}, status=200)
    responses.add(
        responses.GET, f"{_GRAPH}/media1", json={"permalink": "https://instagram.com/p/media1"}, status=200
    )
    connector = InstagramConnector()
    outcome = connector.publish(_ctx("image", [_image()]), PublishState())
    assert outcome.done is True
    assert outcome.remote_id == "media1"
    assert outcome.permalink == "https://instagram.com/p/media1"


@responses.activate
def test_reels_publish_polls_until_finished():
    responses.add(responses.POST, f"{_GRAPH}/ig123/media", json={"id": "container1"}, status=200)
    connector = InstagramConnector()
    outcome = connector.publish(_ctx("short_video", [_video()]), PublishState())
    assert outcome.done is False
    assert outcome.state.step == "processing"
    assert outcome.retry_after_s == 5

    responses.add(responses.GET, f"{_GRAPH}/container1", json={"status_code": "IN_PROGRESS"}, status=200)
    still_going = connector.publish(_ctx("short_video", [_video()]), outcome.state)
    assert still_going.done is False

    responses.add(responses.GET, f"{_GRAPH}/container1", json={"status_code": "FINISHED"}, status=200)
    responses.add(responses.POST, f"{_GRAPH}/ig123/media_publish", json={"id": "media2"}, status=200)
    responses.add(
        responses.GET, f"{_GRAPH}/media2", json={"permalink": "https://instagram.com/reel/media2"}, status=200
    )
    done = connector.publish(_ctx("short_video", [_video()]), still_going.state)
    assert done.done is True
    assert done.remote_id == "media2"

    # media_type=REELS must have been sent on the container-creation call.
    first_call_body = responses.calls[0].request.body
    assert "media_type=REELS" in first_call_body


@responses.activate
def test_carousel_creates_child_containers_then_parent():
    responses.add(responses.POST, f"{_GRAPH}/ig123/media", json={"id": "child1"}, status=200)
    responses.add(responses.POST, f"{_GRAPH}/ig123/media", json={"id": "child2"}, status=200)
    responses.add(responses.POST, f"{_GRAPH}/ig123/media", json={"id": "parent1"}, status=200)
    responses.add(responses.POST, f"{_GRAPH}/ig123/media_publish", json={"id": "media3"}, status=200)
    responses.add(responses.GET, f"{_GRAPH}/media3", json={"permalink": "https://instagram.com/p/media3"}, status=200)

    connector = InstagramConnector()
    outcome = connector.publish(_ctx("image", [_image("a.jpg"), _image("b.jpg")]), PublishState())

    assert outcome.done is True
    assert outcome.remote_id == "media3"
    parent_call_body = responses.calls[2].request.body
    assert "media_type=CAROUSEL" in parent_call_body
    assert "children=child1%2Cchild2" in parent_call_body
