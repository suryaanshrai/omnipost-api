"""Entry point for putting a PostTarget on the publish queue. Called from the
API (scheduling/publishing a post) and from the reconciler (recovering
anything the queue lost)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from omnipost_api.models import PostTarget, PublishAttempt

from . import queue as job_queue

# How far ahead of a scheduled publish to check the channel still works, so a
# broken connection surfaces as a notice before the post fails, not after.
PREFLIGHT_LEAD_TIME = timedelta(minutes=30)


def schedule_post_target(post_target: PostTarget, run_at=None) -> PublishAttempt:
    """Creates the first PublishAttempt for this target's current epoch and
    enqueues it. Safe to call again after a failed/canceled target is
    retried — the caller is expected to have bumped `post_target.epoch`
    first (see views.PostViewSet.publish) so the new attempt's idempotency
    key differs from the old one."""
    run_at = run_at or timezone.now()
    post_target.status = PostTarget.STATUS_SCHEDULED
    post_target.run_at = run_at
    post_target.save(update_fields=["status", "run_at", "updated_at"])

    attempt = PublishAttempt.objects.create(
        post_target=post_target,
        epoch=post_target.epoch,
        attempt_number=1,
        status=PublishAttempt.STATUS_QUEUED,
        run_at=run_at,
    )
    job_queue.enqueue_attempt_at(attempt.pk, run_at)

    preflight_at = run_at - PREFLIGHT_LEAD_TIME
    if preflight_at > timezone.now():
        job_queue.enqueue_preflight_at(post_target.pk, preflight_at)
    # A post scheduled sooner than PREFLIGHT_LEAD_TIME skips the advance
    # check — the publish attempt's own AUTH_EXPIRED handling
    # (jobs/publisher.py) is the only warning window that fits.

    return attempt
