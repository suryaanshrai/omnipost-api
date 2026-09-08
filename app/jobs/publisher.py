"""The job engine's core: run one PublishAttempt to completion or to its next
resumption point.

Replaces omnipost_api.models' old run_action()/send_request(): instead of
scheduling every HTTP step independently at a fixed time offset (so step 2
fired 20s after step 1 whether or not step 1 succeeded), one PublishAttempt
row is the unit of work, and a connector's publish() is called again with
its own persisted state until it reports done=True or raises.

Idempotency: `SELECT ... FOR UPDATE` claims the row, and a status check right
after acquiring the lock is what makes it safe to enqueue this function twice
for the same attempt_id (the reconciler does this deliberately rather than
try to track "is a job already in flight" — see reconciler.py).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from connectors.base import PublishState
from connectors.errors import ConnectorError
from connectors.registry import get as get_connector
from django.db import transaction
from django.utils import timezone
from omnipost_api.models import Post, PostTarget, PublishAttempt

from . import queue as job_queue
from .backoff import DEFAULT_POLL_DELAY_S, MAX_RETRIES, backoff_seconds
from .context import build_context

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {PublishAttempt.STATUS_SUCCEEDED, PublishAttempt.STATUS_FAILED}


def run_publish_attempt(attempt_id: int) -> None:
    with transaction.atomic():
        try:
            attempt = PublishAttempt.objects.select_for_update().select_related(
                "post_target", "post_target__channel", "post_target__post"
            ).get(pk=attempt_id)
        except PublishAttempt.DoesNotExist:
            logger.warning("run_publish_attempt: attempt %s no longer exists", attempt_id)
            return

        if attempt.status in _TERMINAL_STATUSES:
            # Already handled by an earlier delivery of this job — the
            # reconciler enqueues optimistically, so duplicate deliveries are
            # expected and must be no-ops, not double-publishes.
            return

        post_target = attempt.post_target
        if post_target.epoch != attempt.epoch:
            # A newer epoch (user retried/rescheduled) superseded this
            # attempt while it sat in the queue.
            attempt.status = PublishAttempt.STATUS_FAILED
            attempt.error_class = "PERMANENT"
            attempt.error_detail = "Superseded by a newer attempt epoch."
            attempt.finished_at = timezone.now()
            attempt.save(update_fields=["status", "error_class", "error_detail", "finished_at"])
            return

        attempt.status = PublishAttempt.STATUS_RUNNING
        if attempt.started_at is None:
            attempt.started_at = timezone.now()
        attempt.save(update_fields=["status", "started_at"])

        state = PublishState(step=attempt.state_step, data=dict(attempt.state_data))
        connector = get_connector(post_target.channel.connector_slug)
        ctx = build_context(post_target)

        try:
            outcome = connector.publish(ctx, state)
        except ConnectorError as exc:
            _handle_error(attempt, post_target, exc)
            return
        except Exception as exc:  # noqa: BLE001 - genuinely unknown failure, treat as transient
            logger.exception("Unhandled exception in connector.publish for attempt %s", attempt_id)
            _handle_unexpected_error(attempt, post_target, exc)
            return

        if outcome.done:
            _mark_succeeded(attempt, post_target, outcome)
        else:
            _mark_waiting(attempt, outcome)


def _mark_succeeded(attempt: PublishAttempt, post_target: PostTarget, outcome) -> None:
    attempt.status = PublishAttempt.STATUS_SUCCEEDED
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["status", "finished_at"])

    post_target.status = PostTarget.STATUS_PUBLISHED
    post_target.remote_id = outcome.remote_id or ""
    post_target.permalink = outcome.permalink or ""
    post_target.published_at = timezone.now()
    post_target.save(update_fields=["status", "remote_id", "permalink", "published_at", "updated_at"])

    _recompute_post_status(post_target.post_id)

    if post_target.remote_id:
        job_queue.enqueue_metrics_poll(post_target.pk)


def _mark_waiting(attempt: PublishAttempt, outcome) -> None:
    """Not an error — the connector needs to be called again (e.g. "media
    still processing, check back in a few seconds"). Same attempt row,
    same attempt_number: this isn't a retry, it's mid-flight progress."""
    attempt.state_step = outcome.state.step if outcome.state else attempt.state_step
    attempt.state_data = outcome.state.data if outcome.state else attempt.state_data
    attempt.status = PublishAttempt.STATUS_WAITING
    delay = outcome.retry_after_s or DEFAULT_POLL_DELAY_S
    attempt.run_at = timezone.now() + timedelta(seconds=delay)
    attempt.save(update_fields=["state_step", "state_data", "status", "run_at"])
    job_queue.enqueue_attempt_at(attempt.pk, attempt.run_at)


def _handle_error(attempt: PublishAttempt, post_target: PostTarget, exc: ConnectorError) -> None:
    attempt.status = PublishAttempt.STATUS_FAILED
    attempt.error_class = exc.error_class.value
    attempt.error_detail = exc.safe_detail
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["status", "error_class", "error_detail", "finished_at"])
    logger.warning(
        "Publish attempt %s failed (%s): %s", attempt.pk, exc.error_class.value, exc.raw_detail
    )

    if exc.error_class.value == "AUTH_EXPIRED":
        channel = post_target.channel
        channel.health = channel.HEALTH_BROKEN
        channel.health_detail = "Authentication expired — reconnect this channel."
        channel.save(update_fields=["health", "health_detail", "updated_at"])

    if exc.error_class.retryable and attempt.attempt_number < MAX_RETRIES:
        _schedule_retry(attempt, post_target, retry_after=exc.retry_after)
    else:
        post_target.status = PostTarget.STATUS_FAILED
        post_target.save(update_fields=["status", "updated_at"])
        _recompute_post_status(post_target.post_id)


def _handle_unexpected_error(attempt: PublishAttempt, post_target: PostTarget, exc: Exception) -> None:
    attempt.status = PublishAttempt.STATUS_FAILED
    attempt.error_class = "TRANSIENT"
    attempt.error_detail = "An unexpected error occurred."
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["status", "error_class", "error_detail", "finished_at"])

    if attempt.attempt_number < MAX_RETRIES:
        _schedule_retry(attempt, post_target, retry_after=None)
    else:
        post_target.status = PostTarget.STATUS_FAILED
        post_target.save(update_fields=["status", "updated_at"])
        _recompute_post_status(post_target.post_id)


def _schedule_retry(attempt: PublishAttempt, post_target: PostTarget, retry_after: float | None) -> None:
    next_attempt = PublishAttempt.objects.create(
        post_target=post_target,
        epoch=attempt.epoch,
        attempt_number=attempt.attempt_number + 1,
        status=PublishAttempt.STATUS_QUEUED,
        run_at=timezone.now() + timedelta(seconds=backoff_seconds(attempt.attempt_number + 1, retry_after)),
    )
    job_queue.enqueue_attempt_at(next_attempt.pk, next_attempt.run_at)


def _recompute_post_status(post_id: int) -> None:
    post = Post.objects.get(pk=post_id)
    statuses = list(post.targets.values_list("status", flat=True))
    if not statuses:
        return
    if all(s in (PostTarget.STATUS_PUBLISHED, PostTarget.STATUS_CANCELED) for s in statuses) and any(
        s == PostTarget.STATUS_PUBLISHED for s in statuses
    ):
        post.status = Post.STATUS_PUBLISHED
    elif all(s in (PostTarget.STATUS_FAILED, PostTarget.STATUS_CANCELED) for s in statuses):
        post.status = Post.STATUS_FAILED
    elif any(s == PostTarget.STATUS_PUBLISHING for s in statuses):
        post.status = Post.STATUS_PUBLISHING
    else:
        return
    post.save(update_fields=["status", "updated_at"])
