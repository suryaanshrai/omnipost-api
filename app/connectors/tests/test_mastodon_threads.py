import responses

from connectors.base import ChannelCredentials, ContentPart, PublishContext, PublishState
from connectors.platforms.mastodon import MastodonConnector

_STATUSES_URL = "https://mastodon.social/api/v1/statuses"


def _ctx(parts):
    return PublishContext(
        text=parts[0].text,
        media=[],
        post_kind="text",
        credentials=ChannelCredentials(values={"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"}),
        idempotency_key="k",
        parts=parts,
    )


@responses.activate
def test_no_parts_delegates_to_single_status_publish():
    responses.add(responses.POST, _STATUSES_URL, json={"id": "1", "url": "https://mastodon.social/@me/1"}, status=200)
    connector = MastodonConnector()
    ctx = PublishContext(
        text="hello",
        media=[],
        post_kind="text",
        credentials=ChannelCredentials(values={"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"}),
        idempotency_key="k",
    )
    outcome = connector.publish(ctx, PublishState())
    assert outcome.done is True
    assert outcome.remote_id == "1"


@responses.activate
def test_thread_posts_first_part_and_waits_for_the_configured_delay():
    responses.add(
        responses.POST, _STATUSES_URL, json={"id": "100", "url": "https://mastodon.social/@me/100"}, status=200
    )
    connector = MastodonConnector()
    parts = [ContentPart(text="part one", delay_after_s=30), ContentPart(text="part two")]
    outcome = connector.publish(_ctx(parts), PublishState())

    assert outcome.done is False
    assert outcome.retry_after_s == 30
    assert outcome.state.data == {"part_index": 1, "remote_ids": ["100"]}

    sent = responses.calls[0].request
    assert "in_reply_to_id" not in sent.body.decode()


@responses.activate
def test_thread_second_part_replies_to_the_first_and_completes():
    responses.add(
        responses.POST, _STATUSES_URL, json={"id": "200", "url": "https://mastodon.social/@me/200"}, status=200
    )
    connector = MastodonConnector()
    parts = [ContentPart(text="part one"), ContentPart(text="part two")]
    state = PublishState(step="thread_part", data={"part_index": 1, "remote_ids": ["100"]})

    outcome = connector.publish(_ctx(parts), state)

    assert outcome.done is True
    assert outcome.remote_id == "100"  # the thread's root post, not the last part
    sent_body = responses.calls[0].request.body.decode()
    assert '"in_reply_to_id": "100"' in sent_body


@responses.activate
def test_thread_part_rate_limited_raises_retryable_error():
    from connectors.errors import RateLimited

    responses.add(responses.POST, _STATUSES_URL, json={}, status=429, headers={"Retry-After": "45"})
    connector = MastodonConnector()
    parts = [ContentPart(text="part one"), ContentPart(text="part two")]

    try:
        connector.publish(_ctx(parts), PublishState())
        raise AssertionError("expected RateLimited")
    except RateLimited as exc:
        assert exc.retry_after == 45.0
