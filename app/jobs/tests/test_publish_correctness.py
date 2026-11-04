"""Regression tests for exactly-once publishing: no duplicate posts, no posts
after cancel, retries that resume, and tokens that get refreshed."""

import time
from datetime import timedelta

import requests
import responses
from connectors.base import PublishOutcome, PublishState
from connectors.errors import RateLimited, TransientError
from connectors.platforms.x import XConnector
from django.utils import timezone
from omnipost_api.models import Channel, Post, PostTarget, PublishAttempt

from jobs import publisher
from jobs.health import refresh_expiring_channels
from jobs.publisher import expire_stale_running, run_publish_attempt
from jobs.reconciler import reconcile_once
from jobs.scheduler import schedule_post_target

_STATUSES = "https://mastodon.social/api/v1/statuses"


@responses.activate
def test_a_duplicate_delivery_during_the_platform_call_does_not_post_twice(post_target, enqueued, monkeypatch):
    calls = []
    from connectors.platforms.mastodon import MastodonConnector

    attempt = schedule_post_target(post_target)

    def publish(self, ctx, state):
        calls.append(1)
        # A second worker gets the same job while this call is in flight.
        run_publish_attempt(attempt.pk)
        return PublishOutcome(done=True, remote_id="1")

    monkeypatch.setattr(MastodonConnector, "publish", publish)
    run_publish_attempt(attempt.pk)

    assert len(calls) == 1
    post_target.refresh_from_db()
    assert post_target.status == PostTarget.STATUS_PUBLISHED


def test_running_status_is_committed_before_the_platform_call(post_target, enqueued, monkeypatch):
    from connectors.platforms.mastodon import MastodonConnector

    attempt = schedule_post_target(post_target)
    seen = {}

    def publish(self, ctx, state):
        seen["status"] = PublishAttempt.objects.get(pk=attempt.pk).status
        seen["target"] = PostTarget.objects.get(pk=post_target.pk).status
        return PublishOutcome(done=True, remote_id="1")

    monkeypatch.setattr(MastodonConnector, "publish", publish)
    run_publish_attempt(attempt.pk)
    assert seen == {"status": PublishAttempt.STATUS_RUNNING, "target": PostTarget.STATUS_PUBLISHING}


def test_an_attempt_whose_worker_died_is_never_rerun(post_target, enqueued, monkeypatch):
    from connectors.platforms.mastodon import MastodonConnector

    attempt = schedule_post_target(post_target)
    PublishAttempt.objects.filter(pk=attempt.pk).update(
        status=PublishAttempt.STATUS_RUNNING, started_at=timezone.now() - timedelta(hours=1)
    )
    monkeypatch.setattr(MastodonConnector, "publish", lambda *a: (_ for _ in ()).throw(AssertionError("re-ran")))

    run_publish_attempt(attempt.pk)  # a duplicate delivery: no-op
    result = reconcile_once()

    attempt.refresh_from_db()
    post_target.refresh_from_db()
    assert result["stale_running"] == 1
    assert attempt.status == PublishAttempt.STATUS_FAILED
    assert attempt.error_class == "UNKNOWN_OUTCOME"
    assert post_target.status == PostTarget.STATUS_FAILED
    assert not any(kind == "now" and pk == attempt.pk for kind, pk in enqueued)


def test_recent_running_attempts_are_left_alone(post_target, enqueued):
    attempt = schedule_post_target(post_target)
    PublishAttempt.objects.filter(pk=attempt.pk).update(status=PublishAttempt.STATUS_RUNNING, started_at=timezone.now())
    assert expire_stale_running() == 0


@responses.activate
def test_cancelled_post_does_not_publish(post_target, enqueued, django_user_model):
    from omnipost_api.models import Membership
    from rest_framework.test import APIClient

    user = django_user_model.objects.create_user(username="cancel", password="x")
    Membership.objects.create(user=user, workspace=post_target.post.workspace, role=Membership.ROLE_OWNER)
    responses.add(responses.POST, _STATUSES, json={"id": "1", "url": "u"})
    attempt = schedule_post_target(post_target)

    client = APIClient()
    client.force_authenticate(user)
    assert client.post(f"/posts/{post_target.post_id}/cancel/").status_code == 200

    run_publish_attempt(attempt.pk)
    assert len(responses.calls) == 0
    post_target.refresh_from_db()
    assert post_target.status == PostTarget.STATUS_CANCELED


def test_retry_resumes_from_the_last_saved_step(post_target, enqueued, monkeypatch):
    from connectors.platforms.mastodon import MastodonConnector

    seen_steps = []

    def publish(self, ctx, state):
        seen_steps.append(state.step)
        if state.step == "start":
            return PublishOutcome(done=False, state=PublishState(step="part_2", data={"posted": ["1"]}))
        if len(seen_steps) == 2:
            raise TransientError("blip")
        return PublishOutcome(done=True, remote_id="1")

    monkeypatch.setattr(MastodonConnector, "publish", publish)
    first = schedule_post_target(post_target)
    run_publish_attempt(first.pk)  # start -> waiting at part_2
    run_publish_attempt(first.pk)  # part_2 -> transient failure -> retry
    retry = PublishAttempt.objects.get(post_target=post_target, attempt_number=2)
    assert retry.state_step == "part_2"
    assert retry.state_data == {"posted": ["1"]}
    run_publish_attempt(retry.pk)
    assert seen_steps == ["start", "part_2", "part_2"]


def test_platform_still_processing_after_an_hour_fails_cleanly(post_target, enqueued, monkeypatch):
    from connectors.platforms.mastodon import MastodonConnector

    monkeypatch.setattr(
        MastodonConnector, "publish", lambda *a: PublishOutcome(done=False, state=PublishState(step="processing"))
    )
    attempt = schedule_post_target(post_target)
    PublishAttempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(hours=2))
    run_publish_attempt(attempt.pk)
    attempt.refresh_from_db()
    assert attempt.status == PublishAttempt.STATUS_FAILED
    post_target.refresh_from_db()
    assert post_target.status == PostTarget.STATUS_FAILED


def test_rescheduling_a_failed_target_starts_a_new_epoch(post_target, enqueued):
    first = schedule_post_target(post_target)
    PublishAttempt.objects.filter(pk=first.pk).update(status=PublishAttempt.STATUS_FAILED)
    PostTarget.objects.filter(pk=post_target.pk).update(status=PostTarget.STATUS_FAILED)
    post_target.refresh_from_db()

    second = schedule_post_target(post_target)  # used to raise IntegrityError
    assert second.epoch == first.epoch + 1


def test_finished_but_unsynced_target_is_not_republished(post_target, enqueued):
    attempt = schedule_post_target(post_target, run_at=timezone.now() - timedelta(minutes=5))
    PublishAttempt.objects.filter(pk=attempt.pk).update(status=PublishAttempt.STATUS_SUCCEEDED)
    reconcile_once()
    post_target.refresh_from_db()
    assert post_target.status == PostTarget.STATUS_PUBLISHED
    assert PublishAttempt.objects.filter(post_target=post_target).count() == 1
    assert Post.objects.get(pk=post_target.post_id).status == Post.STATUS_PUBLISHED


@responses.activate
def test_x_rate_limit_reset_is_a_timestamp_not_seconds():
    from connectors.base import ChannelCredentials, PublishContext

    reset = int(time.time()) + 120
    responses.add(
        responses.POST,
        "https://api.twitter.com/2/tweets",
        status=429,
        json={},
        headers={"x-rate-limit-reset": str(reset)},
    )
    ctx = PublishContext(
        text="hi",
        media=[],
        post_kind="text",
        credentials=ChannelCredentials(values={"ACCESS_TOKEN": "t"}),
        idempotency_key="k",
    )
    try:
        XConnector().publish(ctx, PublishState())
    except RateLimited as exc:
        assert 60 < exc.retry_after <= 121
    else:
        raise AssertionError("expected RateLimited")


def test_backoff_caps_huge_platform_waits():
    from jobs.backoff import MAX_RETRY_AFTER_S, backoff_seconds

    assert backoff_seconds(1, retry_after=10**9) == MAX_RETRY_AFTER_S


# --- token refresh ---------------------------------------------------------------


def _x_channel(workspace, expires_at):
    channel = Channel.objects.create(
        workspace=workspace, connector_slug="x", display_name="X", token_expires_at=expires_at
    )
    channel.set_credentials({"ACCESS_TOKEN": "old", "REFRESH_TOKEN": "r1", "CLIENT_ID": "c", "CLIENT_SECRET": "s"})
    return channel


@responses.activate
def test_refresh_sweep_refreshes_x_and_records_the_new_expiry(workspace):
    channel = _x_channel(workspace, timezone.now() + timedelta(minutes=30))
    responses.add(
        responses.POST,
        "https://api.twitter.com/2/oauth2/token",
        json={"access_token": "new", "refresh_token": "r2", "expires_in": 7200},
    )
    assert refresh_expiring_channels() == {"refreshed": 1, "broken": 0}
    channel.refresh_from_db()
    assert channel.get_credentials()["ACCESS_TOKEN"] == "new"
    assert channel.token_expires_at > timezone.now() + timedelta(hours=1)


@responses.activate
def test_refresh_sweep_survives_a_platform_outage(workspace):
    _x_channel(workspace, timezone.now() + timedelta(minutes=30))
    responses.add(responses.POST, "https://api.twitter.com/2/oauth2/token", body=requests.ConnectionError("down"))
    assert refresh_expiring_channels() == {"refreshed": 0, "broken": 0}


@responses.activate
def test_a_401_refreshes_the_token_once_and_retries(workspace, enqueued):
    channel = _x_channel(workspace, None)
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hello")
    target = PostTarget.objects.create(post=post, channel=channel)
    responses.add(responses.POST, "https://api.twitter.com/2/tweets", status=401, json={})
    responses.add(
        responses.POST,
        "https://api.twitter.com/2/oauth2/token",
        json={"access_token": "new", "refresh_token": "r2", "expires_in": 7200},
    )
    attempt = schedule_post_target(target)
    run_publish_attempt(attempt.pk)

    channel.refresh_from_db()
    assert channel.health == Channel.HEALTH_HEALTHY
    retry = PublishAttempt.objects.get(post_target=target, attempt_number=2)
    assert retry.state_data.get("_refreshed") is True
    assert ("at", retry.pk) in enqueued

    # A second 401 after the refresh marks the channel broken.
    run_publish_attempt(retry.pk)
    channel.refresh_from_db()
    assert channel.health == Channel.HEALTH_BROKEN
    assert publisher.MAX_RETRIES > 2
