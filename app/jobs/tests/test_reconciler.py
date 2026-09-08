from datetime import timedelta

from django.utils import timezone
from omnipost_api.models import PostTarget, PublishAttempt

from jobs.reconciler import reconcile_once
from jobs.scheduler import schedule_post_target


def test_recovers_orphaned_scheduled_target_with_no_attempt(post_target, enqueued):
    # Simulates a Redis flush: the target was marked scheduled but its
    # PublishAttempt row (and the RQ job) never made it / was lost.
    post_target.status = PostTarget.STATUS_SCHEDULED
    post_target.run_at = timezone.now() - timedelta(minutes=1)
    post_target.save()

    result = reconcile_once()

    assert result["recovered_targets"] == 1
    assert PublishAttempt.objects.filter(post_target=post_target).exists()


def test_does_not_touch_targets_with_a_live_attempt(post_target, enqueued):
    attempt = schedule_post_target(post_target, run_at=timezone.now() - timedelta(minutes=1))

    result = reconcile_once()

    # The live (queued) attempt is due, so it gets requeued — but no NEW
    # attempt should be created for the same target.
    assert result["recovered_targets"] == 0
    assert PublishAttempt.objects.filter(post_target=post_target).count() == 1
    assert ("now", attempt.pk) in enqueued


def test_ignores_targets_not_yet_due(post_target, enqueued):
    post_target.status = PostTarget.STATUS_SCHEDULED
    post_target.run_at = timezone.now() + timedelta(hours=1)
    post_target.save()

    result = reconcile_once()

    assert result["recovered_targets"] == 0
    assert not PublishAttempt.objects.filter(post_target=post_target).exists()


def test_requeues_a_due_waiting_attempt(post_target, enqueued):
    attempt = PublishAttempt.objects.create(
        post_target=post_target,
        epoch=1,
        attempt_number=1,
        status=PublishAttempt.STATUS_WAITING,
        run_at=timezone.now() - timedelta(seconds=5),
    )
    result = reconcile_once()
    assert result["requeued_attempts"] == 1
    assert ("now", attempt.pk) in enqueued
