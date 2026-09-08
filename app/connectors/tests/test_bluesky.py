import responses

from connectors.base import ChannelCredentials, MediaRef, PublishContext, PublishState
from connectors.platforms.bluesky import BlueskyConnector


def _ctx(text: str, media: list[MediaRef] | None = None) -> PublishContext:
    return PublishContext(
        text=text,
        media=media or [],
        post_kind="image" if media else "text",
        credentials=ChannelCredentials(values={"IDENTIFIER": "me.bsky.social", "APP_PASSWORD": "app-pw"}),
        idempotency_key="k",
    )


@responses.activate
def test_text_only_publish():
    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"accessJwt": "jwt", "did": "did:plc:abc"},
        status=200,
    )
    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        json={"uri": "at://did:plc:abc/app.bsky.feed.post/xyz123"},
        status=200,
    )
    connector = BlueskyConnector()
    outcome = connector.publish(_ctx("hello bluesky"), PublishState())
    assert outcome.done is True
    assert outcome.remote_id == "at://did:plc:abc/app.bsky.feed.post/xyz123"
    assert outcome.permalink == "https://bsky.app/profile/me.bsky.social/post/xyz123"


@responses.activate
def test_image_publish_uploads_blob_then_creates_record():
    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"accessJwt": "jwt", "did": "did:plc:abc"},
        status=200,
    )
    responses.add(responses.GET, "https://example.com/pic.jpg", body=b"fake-image-bytes", status=200)
    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.repo.uploadBlob",
        json={"blob": {"$type": "blob", "ref": {"$link": "bafyabc"}, "mimeType": "image/jpeg", "size": 17}},
        status=200,
    )
    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        json={"uri": "at://did:plc:abc/app.bsky.feed.post/withimg"},
        status=200,
    )

    media = [MediaRef(url="https://example.com/pic.jpg", kind="image", mime_type="image/jpeg", alt_text="a cat")]
    connector = BlueskyConnector()
    outcome = connector.publish(_ctx("look at this", media=media), PublishState())

    assert outcome.done is True
    create_record_call = responses.calls[-1]
    import json

    body = json.loads(create_record_call.request.body)
    assert body["record"]["embed"]["$type"] == "app.bsky.embed.images"
    assert body["record"]["embed"]["images"][0]["alt"] == "a cat"


@responses.activate
def test_auth_expired_on_bad_credentials():
    from connectors.errors import AuthExpired

    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"error": "AuthenticationRequired"},
        status=401,
    )
    connector = BlueskyConnector()
    import pytest

    with pytest.raises(AuthExpired):
        connector.publish(_ctx("hi"), PublishState())


@responses.activate
def test_fetch_metrics_returns_normalized_counts():
    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"accessJwt": "jwt", "did": "did:plc:abc"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://bsky.social/xrpc/app.bsky.feed.getPostThread",
        json={"thread": {"post": {"likeCount": 5, "repostCount": 2, "replyCount": 1}}},
        status=200,
    )
    connector = BlueskyConnector()
    credentials = ChannelCredentials(values={"IDENTIFIER": "me.bsky.social", "APP_PASSWORD": "app-pw"})
    metrics = connector.fetch_metrics(credentials, "at://did:plc:abc/app.bsky.feed.post/xyz123")
    assert metrics == {"likes": 5, "shares": 2, "comments": 1, "impressions": None}


@responses.activate
def test_fetch_metrics_returns_empty_dict_for_deleted_post():
    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"accessJwt": "jwt", "did": "did:plc:abc"},
        status=200,
    )
    responses.add(
        responses.GET,
        "https://bsky.social/xrpc/app.bsky.feed.getPostThread",
        json={"error": "NotFound"},
        status=400,
    )
    connector = BlueskyConnector()
    credentials = ChannelCredentials(values={"IDENTIFIER": "me.bsky.social", "APP_PASSWORD": "app-pw"})
    assert connector.fetch_metrics(credentials, "at://did:plc:abc/app.bsky.feed.post/gone") == {}


@responses.activate
def test_link_in_text_produces_facet():
    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.server.createSession",
        json={"accessJwt": "jwt", "did": "did:plc:abc"},
        status=200,
    )
    responses.add(
        responses.POST,
        "https://bsky.social/xrpc/com.atproto.repo.createRecord",
        json={"uri": "at://did:plc:abc/app.bsky.feed.post/xyz"},
        status=200,
    )
    connector = BlueskyConnector()
    connector.publish(_ctx("check this out https://example.com/page"), PublishState())

    import json

    body = json.loads(responses.calls[-1].request.body)
    facets = body["record"]["facets"]
    assert len(facets) == 1
    assert facets[0]["features"][0]["uri"] == "https://example.com/page"
