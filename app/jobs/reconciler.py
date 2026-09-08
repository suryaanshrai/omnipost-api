"""Recovers scheduled work that the queue lost — a flushed Redis, a crashed
worker that never got to enqueue a retry, a job that silently vanished.
Postgres (PostTarget.run_at, PublishAttempt.run_at) is the source of truth;
Redis is disposable transport. Run continuously via the `scheduler`
OMNIPOST_ROLE (see docker/entrypoint.sh) or on a cron.

Re-enqueueing something that's already been picked up is intentionally safe:
run_publish_attempt() checks status under a row lock before doing any work,
so a duplicate delivery is a no-op, not a double-publish.
"""

from __future__ import annotations

import logging

from django.utils import timezone
from omnipost_api.models import PostTarget, PublishAttempt

from . import queue as job_queue

logger = logging.getLogger(__name__)

_LIVE_ATTEMPT_STATUSES = [PublishAttempt.STATUS_QUEUED, PublishAttempt.STATUS_RUNNING, PublishAttempt.STATUS_WAITING]


def reconcile_once() -> dict[str, int]:
    now = timezone.now()
    recovered_targets = _recover_orphaned_targets(now)
    requeued_attempts = _requeue_due_attempts(now)
    return {"recovered_targets": recovered_targets, "requeued_attempts": requeued_attempts}


def _recover_orphaned_targets(now) -> int:
    """A PostTarget marked scheduled-and-due with no live attempt at all
    means its original attempt was lost before ever reaching Postgres, or
    every attempt for its current epoch already went terminal without the
    target being updated (shouldn't happen, but this is the safety net)."""
    from . import scheduler

    due = PostTarget.objects.filter(status=PostTarget.STATUS_SCHEDULED, run_at__lte=now)
    count = 0
    for target in due:
        has_live_attempt = target.attempts.filter(
            epoch=target.epoch, status__in=_LIVE_ATTEMPT_STATUSES
        ).exists()
        if not has_live_attempt:
            scheduler.schedule_post_target(target, run_at=target.run_at)
            count += 1
            logger.info("Reconciler recovered orphaned PostTarget %s", target.pk)
    return count


def _requeue_due_attempts(now) -> int:
    due = PublishAttempt.objects.filter(status__in=_LIVE_ATTEMPT_STATUSES, run_at__lte=now)
    count = 0
    for attempt in due:
        job_queue.enqueue_attempt_now(attempt.pk)
        count += 1
    return count
