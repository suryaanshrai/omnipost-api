"""The job engine's core: run one PublishAttempt to completion or to its next
resumption point.

One PublishAttempt row is the unit of work, and a connector's publish() is
called again with its own persisted state until it reports done=True or
raises.

Exactly-once is the goal, so the platform call never happens inside a
database transaction:

1. claim: lock the row, check it's still wanted (right epoch, not
   cancelled), mark it RUNNING and commit;
2. call the platform with no transaction open;
3. record the outcome in a fresh transaction.

If the worker dies during step 2 we can't know whether the platform accepted
the post, so a RUNNING attempt is never re-run. After STALE_RUNNING_AFTER it
is marked failed with "check the platform before retrying"; retrying is the
user's call. Enqueueing the same attempt twice is still safe: the second
delivery finds it RUNNING or finished and does nothing.
"""

from __future__ import annotations

import logging
from datetime import timedelta

import requests
from connectors.base import PublishState
from connectors.errors import ConnectorError, TransientError
from connectors.registry import get as get_connector
from django.db import transaction
from django.utils import timezone
from omnipost_api.models import Post, PostTarget, PublishAttempt
from omnipost_api.services import notify

from . import queue as job_queue
from .backoff import DEFAULT_POLL_DELAY_S, MAX_RETRIES, backoff_seconds
from .context import build_context

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {PublishAttempt.STATUS_SUCCEEDED, PublishAttempt.STATUS_FAILED}

# Longer than the RQ job timeout (360s), so a RUNNING attempt this old has
# no live worker behind it.
STALE_RUNNING_AFTER = timedelta(minutes=15)
# How long a platform may keep saying "still processing" before we give up.
MAX_PROCESSING_TIME = timedelta(minutes=60)

UNKNOWN_OUTCOME_DETAIL = (
    "We lost contact with the platform while publishing, so this may or may not have posted. "
    "Check the account before retrying."
)


def run_publish_attempt(attempt_id: int) -> None:
    claimed = _claim(attempt_id)
    if claimed is None:
        return
    attempt, post_target, state = claimed

    try:
        connector = get_connector(post_target.channel.connector_slug)
        ctx = build_context(post_target)
        outcome = connector.publish(ctx, state)
    except ConnectorError as exc:
        if exc.error_class.value == "AUTH_EXPIRED" and _refreshed_after_401(attempt, post_target):
            _record(_retry_after_refresh, attempt, post_target)
            return
        _record(_handle_error, attempt, post_target, exc)
        return
    except requests.RequestException as exc:
        # The platform didn't answer: an ordinary transient failure, not a bug.
        logger.warning("Platform unreachable for attempt %s: %s", attempt_id, exc)
        _record(_handle_error, attempt, post_target, TransientError("Couldn't reach the platform. Will retry."))
        return
    except Exception as exc:  # noqa: BLE001 - genuinely unknown failure, treat as transient
        logger.exception("Unhandled exception in connector.publish for attempt %s", attempt_id)
        _record(_handle_unexpected_error, attempt, post_target, exc)
        return

    if outcome.done:
        _record(_mark_succeeded, attempt, post_target, outcome)
    else:
        _record(_mark_waiting, attempt, post_target, outcome)


def _record(handler, *args) -> None:
    """Step 3: write the outcome in one transaction, then enqueue whatever
    comes next. Enqueueing after the commit means a worker can't pick up the
    follow-up job before the row it reads is saved."""
    with transaction.atomic():
        followup = handler(*args)
    if followup is not None:
        followup()


def _refreshed_after_401(attempt: PublishAttempt, post_target: PostTarget) -> bool:
    """On a 401, a connector that can refresh its token gets one refresh and
    one immediate retry before the channel is marked broken."""
    import requests

    from .health import refresh_channel, supports_refresh

    if attempt.attempt_number >= MAX_RETRIES or attempt.state_data.get("_refreshed"):
        return False
    if not supports_refresh(get_connector(post_target.channel.connector_slug)):
        return False
    try:
        return refresh_channel(post_target.channel)
    except (ConnectorError, requests.RequestException):
        return False


def _retry_after_refresh(attempt: PublishAttempt, post_target: PostTarget):
    _finish(attempt, PublishAttempt.STATUS_FAILED, "AUTH_EXPIRED", "Token expired; refreshed it and retrying.")
    attempt.state_data = {**attempt.state_data, "_refreshed": True}
    return _schedule_retry(attempt, post_target, retry_after=1.0)


def _claim(attempt_id: int) -> tuple[PublishAttempt, PostTarget, PublishState] | None:
    """Step 1. Returns what step 2 needs, or None if there's nothing to do."""
    with transaction.atomic():
        try:
            attempt = (
                PublishAttempt.objects.select_for_update()
                .select_related("post_target", "post_target__channel", "post_target__post")
                .get(pk=attempt_id)
            )
        except PublishAttempt.DoesNotExist:
            logger.warning("run_publish_attempt: attempt %s no longer exists", attempt_id)
            return None

        if attempt.status in _TERMINAL_STATUSES:
            return None  # an earlier delivery of this job already finished it
        if attempt.status == PublishAttempt.STATUS_RUNNING:
            # Another worker has it, or one died mid-call. Either way, never
            # call the platform a second time; the stale sweep handles it.
            return None

        post_target = attempt.post_target
        if post_target.epoch != attempt.epoch or post_target.status == PostTarget.STATUS_CANCELED:
            # Retried, rescheduled or cancelled while this sat in the queue.
            _finish(attempt, PublishAttempt.STATUS_FAILED, "PERMANENT", "Superseded or cancelled before it ran.")
            return None

        attempt.status = PublishAttempt.STATUS_RUNNING
        attempt.started_at = attempt.started_at or timezone.now()
        attempt.save(update_fields=["status", "started_at"])
        if post_target.status != PostTarget.STATUS_PUBLISHING:
            post_target.status = PostTarget.STATUS_PUBLISHING
            post_target.save(update_fields=["status", "updated_at"])
            _recompute_post_status(post_target.post_id)
        state = PublishState(step=attempt.state_step, data=dict(attempt.state_data))
    return attempt, post_target, state


def expire_stale_running(now=None) -> int:
    """Marks attempts stuck in RUNNING (their worker died mid-call) as failed
    with an "unknown outcome" message, so the user decides whether to retry.
    Called by the reconciler."""
    now = now or timezone.now()
    count = 0
    stale_ids = PublishAttempt.objects.filter(
        status=PublishAttempt.STATUS_RUNNING, started_at__lt=now - STALE_RUNNING_AFTER
    ).values_list("pk", flat=True)
    for attempt_id in stale_ids:
        with transaction.atomic():
            attempt = (
                PublishAttempt.objects.select_for_update().select_related("post_target").filter(pk=attempt_id).first()
            )
            if attempt is None or attempt.status != PublishAttempt.STATUS_RUNNING:
                continue
            _finish(attempt, PublishAttempt.STATUS_FAILED, "UNKNOWN_OUTCOME", UNKNOWN_OUTCOME_DETAIL)
            target = attempt.post_target
            if target.epoch == attempt.epoch:
                _fail_target(target)
            count += 1
            logger.warning("Attempt %s was stuck running; marked unknown outcome", attempt_id)
    return count


def _finish(attempt: PublishAttempt, status: str, error_class: str = "", detail: str = "") -> None:
    attempt.status = status
    attempt.error_class = error_class
    attempt.error_detail = detail
    attempt.finished_at = timezone.now()
    attempt.save(update_fields=["status", "error_class", "error_detail", "finished_at"])


def _mark_succeeded(attempt: PublishAttempt, post_target: PostTarget, outcome):
    _finish(attempt, PublishAttempt.STATUS_SUCCEEDED)

    post_target.status = PostTarget.STATUS_PUBLISHED
    post_target.remote_id = outcome.remote_id or ""
    post_target.permalink = outcome.permalink or ""
    post_target.published_at = timezone.now()
    post_target.save(update_fields=["status", "remote_id", "permalink", "published_at", "updated_at"])

    _recompute_post_status(post_target.post_id)
    notify.post_published(post_target)

    if post_target.remote_id:
        return lambda: job_queue.enqueue_metrics_poll(post_target.pk)
    return None


def _mark_waiting(attempt: PublishAttempt, post_target: PostTarget, outcome):
    """Not an error — the connector needs to be called again (e.g. "media
    still processing, check back in a few seconds"). Same attempt row,
    same attempt_number: this isn't a retry, it's mid-flight progress."""
    if attempt.started_at and timezone.now() - attempt.started_at > MAX_PROCESSING_TIME:
        _finish(
            attempt,
            PublishAttempt.STATUS_FAILED,
            "PERMANENT",
            "The platform was still processing this after an hour. Check the account, then retry.",
        )
        _fail_target(post_target)
        return None
    attempt.state_step = outcome.state.step if outcome.state else attempt.state_step
    attempt.state_data = outcome.state.data if outcome.state else attempt.state_data
    attempt.status = PublishAttempt.STATUS_WAITING
    delay = outcome.retry_after_s or DEFAULT_POLL_DELAY_S
    attempt.run_at = timezone.now() + timedelta(seconds=delay)
    attempt.save(update_fields=["state_step", "state_data", "status", "run_at"])
    run_at = attempt.run_at
    return lambda: job_queue.enqueue_attempt_at(attempt.pk, run_at)


def _handle_error(attempt: PublishAttempt, post_target: PostTarget, exc: ConnectorError):
    _finish(attempt, PublishAttempt.STATUS_FAILED, exc.error_class.value, exc.safe_detail)
    logger.warning(
        "Publish attempt %s failed (%s): %s", attempt.pk, exc.error_class.value, exc.raw_detail
    )

    if exc.error_class.value == "AUTH_EXPIRED":
        channel = post_target.channel
        was_broken = channel.health == channel.HEALTH_BROKEN
        channel.health = channel.HEALTH_BROKEN
        channel.health_detail = "Authentication expired — reconnect this channel."
        channel.save(update_fields=["health", "health_detail", "updated_at"])
        if not was_broken:
            notify.channel_broken(channel)

    if exc.error_class.retryable and attempt.attempt_number < MAX_RETRIES:
        return _schedule_retry(attempt, post_target, retry_after=exc.retry_after)
    _fail_target(post_target)
    return None


def _handle_unexpected_error(attempt: PublishAttempt, post_target: PostTarget, exc: Exception):
    _finish(attempt, PublishAttempt.STATUS_FAILED, "TRANSIENT", "An unexpected error occurred.")
    if attempt.attempt_number < MAX_RETRIES:
        return _schedule_retry(attempt, post_target, retry_after=None)
    _fail_target(post_target)
    return None


def _fail_target(post_target: PostTarget) -> None:
    post_target.status = PostTarget.STATUS_FAILED
    post_target.save(update_fields=["status", "updated_at"])
    _recompute_post_status(post_target.post_id)
    notify.post_failed(post_target)


def _schedule_retry(attempt: PublishAttempt, post_target: PostTarget, retry_after: float | None):
    # The retry resumes from the failed attempt's last saved step, so a
    # thread that failed on part 3 doesn't re-post parts 1 and 2.
    next_attempt = PublishAttempt.objects.create(
        post_target=post_target,
        epoch=attempt.epoch,
        attempt_number=attempt.attempt_number + 1,
        status=PublishAttempt.STATUS_QUEUED,
        state_step=attempt.state_step,
        state_data=attempt.state_data,
        run_at=timezone.now() + timedelta(seconds=backoff_seconds(attempt.attempt_number + 1, retry_after)),
    )
    # Back to "scheduled" while it waits, so it can still be cancelled.
    post_target.status = PostTarget.STATUS_SCHEDULED
    post_target.run_at = next_attempt.run_at
    post_target.save(update_fields=["status", "run_at", "updated_at"])
    return lambda: job_queue.enqueue_attempt_at(next_attempt.pk, next_attempt.run_at)


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
