import responses
from omnipost_api.models import Post, PostTarget, PublishAttempt

from jobs.publisher import run_publish_attempt
from jobs.scheduler import schedule_post_target


def _mock_mastodon_success():
    responses.add(
        responses.POST,
        "https://mastodon.social/api/v1/statuses",
        json={"id": "999", "url": "https://mastodon.social/@me/999"},
        status=200,
    )


def _mock_mastodon_status(status_code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    responses.add(
        responses.POST, "https://mastodon.social/api/v1/statuses", json={}, status=status_code, headers=headers
    )


@responses.activate
def test_successful_publish_marks_everything_published(post_target, enqueued):
    _mock_mastodon_success()
    attempt = schedule_post_target(post_target)
    run_publish_attempt(attempt.pk)

    post_target.refresh_from_db()
    attempt.refresh_from_db()
    assert attempt.status == PublishAttempt.STATUS_SUCCEEDED
    assert post_target.status == PostTarget.STATUS_PUBLISHED
    assert post_target.remote_id == "999"
    assert post_target.post.status == Post.STATUS_PUBLISHED


@responses.activate
def test_calling_publish_twice_does_not_double_post(post_target, enqueued):
    # This is the exact scenario the old system had no protection against:
    # /publish/ called twice enqueued two full chains with no dedupe.
    _mock_mastodon_success()
    attempt = schedule_post_target(post_target)
    run_publish_attempt(attempt.pk)
    run_publish_attempt(attempt.pk)  # duplicate delivery — must be a no-op

    assert len(responses.calls) == 1
    post_target.refresh_from_db()
    assert post_target.status == PostTarget.STATUS_PUBLISHED


@responses.activate
def test_rate_limited_failure_schedules_a_backoff_retry(post_target, enqueued):
    _mock_mastodon_status(429, retry_after=45)
    attempt = schedule_post_target(post_target)
    run_publish_attempt(attempt.pk)

    attempt.refresh_from_db()
    assert attempt.status == PublishAttempt.STATUS_FAILED
    assert attempt.error_class == "RATE_LIMITED"

    post_target.refresh_from_db()
    assert post_target.status == PostTarget.STATUS_SCHEDULED  # not yet terminal — a retry is pending

    retry = PublishAttempt.objects.exclude(pk=attempt.pk).get(post_target=post_target)
    assert retry.attempt_number == 2
    assert retry.epoch == attempt.epoch  # same logical publish, not a new one
    assert ("at", retry.pk) in enqueued


@responses.activate
def test_permanent_failure_does_not_retry(post_target, enqueued):
    _mock_mastodon_status(422)
    attempt = schedule_post_target(post_target)
    run_publish_attempt(attempt.pk)

    attempt.refresh_from_db()
    post_target.refresh_from_db()
    assert attempt.status == PublishAttempt.STATUS_FAILED
    assert attempt.error_class == "PERMANENT"
    assert post_target.status == PostTarget.STATUS_FAILED
    assert PublishAttempt.objects.filter(post_target=post_target).count() == 1


@responses.activate
def test_auth_expired_marks_channel_broken(post_target, enqueued):
    _mock_mastodon_status(401)
    attempt = schedule_post_target(post_target)
    run_publish_attempt(attempt.pk)

    channel = post_target.channel
    channel.refresh_from_db()
    assert channel.health == channel.HEALTH_BROKEN


@responses.activate
def test_retry_exhaustion_gives_up(post_target, enqueued):
    from jobs.backoff import MAX_RETRIES

    for _ in range(MAX_RETRIES + 1):
        _mock_mastodon_status(429, retry_after=0.01)

    attempt = schedule_post_target(post_target)
    run_publish_attempt(attempt.pk)
    for _ in range(MAX_RETRIES - 1):
        latest = PublishAttempt.objects.filter(post_target=post_target).order_by("-attempt_number").first()
        run_publish_attempt(latest.pk)

    post_target.refresh_from_db()
    assert post_target.status == PostTarget.STATUS_FAILED
    assert PublishAttempt.objects.filter(post_target=post_target).count() == MAX_RETRIES


@responses.activate
def test_superseded_epoch_is_not_published(post_target, enqueued):
    # Attempt is enqueued, then the user retries (bumping epoch) before the
    # old attempt runs. The stale attempt must not publish.
    _mock_mastodon_success()
    attempt = schedule_post_target(post_target)

    post_target.epoch += 1
    post_target.save()

    run_publish_attempt(attempt.pk)

    attempt.refresh_from_db()
    assert attempt.status == PublishAttempt.STATUS_FAILED
    assert len(responses.calls) == 0


@responses.activate
def test_multi_step_connector_persists_state_between_calls(post_target, enqueued, monkeypatch):
    from connectors.base import PublishOutcome, PublishState

    call_log = []

    class FakeStatefulConnector:
        slug = "mastodon"
        capabilities = None

        def publish(self, ctx, state):
            call_log.append(state.step)
            if state.step == "start":
                return PublishOutcome(
                    done=False,
                    state=PublishState(step="waiting_for_processing", data={"x": 1}),
                    retry_after_s=0.01,
                )
            return PublishOutcome(done=True, remote_id="r1", permalink="https://x/r1")

    monkeypatch.setattr("jobs.publisher.get_connector", lambda slug: FakeStatefulConnector())

    attempt = schedule_post_target(post_target)
    run_publish_attempt(attempt.pk)

    attempt.refresh_from_db()
    assert attempt.status == PublishAttempt.STATUS_WAITING
    assert attempt.state_step == "waiting_for_processing"
    assert attempt.state_data == {"x": 1}
    assert attempt.attempt_number == 1  # same attempt, not a retry

    run_publish_attempt(attempt.pk)
    attempt.refresh_from_db()
    assert attempt.status == PublishAttempt.STATUS_SUCCEEDED
    assert call_log == ["start", "waiting_for_processing"]
