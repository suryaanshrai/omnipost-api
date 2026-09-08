import pytest
import responses

from connectors.base import ChannelCredentials, PublishContext, PublishState
from connectors.declarative import SpecError, validate_spec
from connectors.errors import AuthExpired, PermanentError, RateLimited
from connectors.registry import get


def _ctx(text: str, **credentials: str) -> PublishContext:
    return PublishContext(
        text=text,
        media=[],
        post_kind="text",
        credentials=ChannelCredentials(values=credentials),
        idempotency_key="test-key",
    )


@responses.activate
def test_mastodon_publish_success():
    responses.add(
        responses.POST,
        "https://mastodon.social/api/v1/statuses",
        json={"id": "12345", "url": "https://mastodon.social/@me/12345"},
        status=200,
    )
    connector = get("mastodon")
    outcome = connector.publish(
        _ctx("hello world", ACCESS_TOKEN="tok", INSTANCE_DOMAIN="mastodon.social"), PublishState()
    )
    assert outcome.done is True
    assert outcome.remote_id == "12345"
    assert outcome.permalink == "https://mastodon.social/@me/12345"

    sent = responses.calls[0].request
    assert sent.headers["Authorization"] == "Bearer tok"


@responses.activate
def test_mastodon_adversarial_caption_reaches_upstream_unmodified():
    # A caption containing the literal variable names must not corrupt the
    # request. This is the exact failure mode replace_keys() had.
    tricky = 'My post about TEXT, ACCESS_TOKEN, and "quotes"'
    responses.add(
        responses.POST,
        "https://mastodon.social/api/v1/statuses",
        json={"id": "1", "url": "https://x/1"},
        status=200,
    )
    connector = get("mastodon")
    connector.publish(_ctx(tricky, ACCESS_TOKEN="tok", INSTANCE_DOMAIN="mastodon.social"), PublishState())

    import json

    sent_body = json.loads(responses.calls[0].request.body)
    assert sent_body["status"] == tricky


@responses.activate
def test_mastodon_401_maps_to_auth_expired():
    responses.add(responses.POST, "https://mastodon.social/api/v1/statuses", json={}, status=401)
    connector = get("mastodon")
    with pytest.raises(AuthExpired):
        connector.publish(_ctx("hi", ACCESS_TOKEN="bad", INSTANCE_DOMAIN="mastodon.social"), PublishState())


@responses.activate
def test_mastodon_429_maps_to_rate_limited_with_retry_after():
    responses.add(
        responses.POST,
        "https://mastodon.social/api/v1/statuses",
        json={},
        status=429,
        headers={"Retry-After": "60"},
    )
    connector = get("mastodon")
    with pytest.raises(RateLimited) as exc_info:
        connector.publish(_ctx("hi", ACCESS_TOKEN="tok", INSTANCE_DOMAIN="mastodon.social"), PublishState())
    assert exc_info.value.retry_after == 60.0


@responses.activate
def test_mastodon_422_maps_to_permanent_not_retried():
    responses.add(responses.POST, "https://mastodon.social/api/v1/statuses", json={}, status=422)
    connector = get("mastodon")
    with pytest.raises(PermanentError):
        connector.publish(_ctx("hi", ACCESS_TOKEN="tok", INSTANCE_DOMAIN="mastodon.social"), PublishState())


@responses.activate
def test_telegram_nested_response_mapping():
    responses.add(
        responses.POST,
        "https://api.telegram.org/bot12345:abc/sendMessage",
        json={"ok": True, "result": {"message_id": 999}},
        status=200,
    )
    connector = get("telegram")
    outcome = connector.publish(_ctx("hi", BOT_TOKEN="12345:abc", CHAT_ID="-100999"), PublishState())
    assert outcome.done is True
    assert outcome.remote_id == 999


@responses.activate
def test_discord_webhook_publish():
    responses.add(
        responses.POST,
        "https://discord.com/api/webhooks/1/token",
        json={"id": "555"},
        status=200,
    )
    connector = get("discord")
    outcome = connector.publish(
        _ctx("hi", WEBHOOK_URL="https://discord.com/api/webhooks/1/token"), PublishState()
    )
    assert outcome.done is True
    assert outcome.remote_id == "555"
    assert responses.calls[0].request.params["wait"] == "true"


def test_spec_missing_request_envelope_is_rejected_at_load_time():
    # This is exactly the bug the old linkedin.json shipped with: a step
    # whose "request" was the raw platform payload with no
    # method/base_url/endpoint wrapper, which raised KeyError only when a
    # user actually tried to publish. Now it fails validate_spec() instead.
    bad_spec = {
        "slug": "broken",
        "display_name": "Broken",
        "instance": ["ACCESS_TOKEN"],
        "actions": {"publish_text": {"steps": [{"request": {"payload": {"text": "{{ TEXT }}"}}}]}},
    }
    with pytest.raises(SpecError, match="missing"):
        validate_spec(bad_spec)


def test_spec_referencing_undefined_variable_is_rejected_at_load_time():
    bad_spec = {
        "slug": "broken",
        "display_name": "Broken",
        "instance": ["ACCESS_TOKEN"],
        "actions": {
            "publish_text": {
                "steps": [
                    {
                        "request": {
                            "method": "POST",
                            "base_url": "https://x",
                            "endpoint": "/y",
                            "payload": {"link": "{{ REEL_URL }}"},
                        }
                    }
                ]
            }
        },
    }
    # This is exactly the old facebook.json bug: REEL_URL referenced but
    # never produced by any step or credential.
    with pytest.raises(SpecError, match="undefined variable"):
        validate_spec(bad_spec)
