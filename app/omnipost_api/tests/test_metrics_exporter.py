import pytest
from django.test import Client, override_settings

from omnipost_api.models import Channel, Post, PostTarget, PublishAttempt


@pytest.fixture(autouse=True)
def fake_rq_queues(monkeypatch):
    """No live Redis is assumed for `pytest` (same "zero setup" goal as the
    sqlite DB fallback in settings.py) — stand in a fake with a fixed depth
    rather than let the exporter's real django_rq.get_queue() try to connect."""

    class _FakeQueue:
        count = 3

    monkeypatch.setattr("omnipost_api.metrics_exporter.django_rq.get_queue", lambda name: _FakeQueue())


def test_metrics_endpoint_is_open_by_default(db):
    response = Client().get("/metrics")
    assert response.status_code == 200
    assert b'omnipost_queue_depth{queue="publish"} 3.0' in response.content


def test_metrics_reflects_publish_attempt_and_channel_counts(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="M")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    target = PostTarget.objects.create(post=post, channel=channel, status=PostTarget.STATUS_PUBLISHED)
    PublishAttempt.objects.create(post_target=target, status=PublishAttempt.STATUS_SUCCEEDED, run_at=target.created_at)

    body = Client().get("/metrics").content.decode()

    assert 'omnipost_publish_attempts_total{connector_slug="mastodon",status="succeeded"} 1.0' in body
    assert 'omnipost_channel_health{connector_slug="mastodon",health="healthy"} 1.0' in body


@override_settings(METRICS_AUTH_TOKEN="secret-token")
def test_metrics_endpoint_requires_token_when_configured(db):
    client = Client()
    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics", headers={"Authorization": "Bearer secret-token"}).status_code == 200
    assert client.get("/metrics?token=secret-token").status_code == 200
