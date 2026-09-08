"""Engagement metrics polling (Phase 7).

A connector's fetch_metrics() is best-effort by design (base.py defaults to
returning {}), so this module never treats a missing/failed fetch as an
error worth retrying — it just skips and tries again on the next sweep.
Postgres (PostMetric rows) is the only place this data lives; Redis is
transport for the poll jobs themselves, same "Postgres is truth" split as
every other part of the job engine.

Two entry points:
  * poll_metrics(post_target_id) — one fetch, enqueued once right after a
    successful publish (jobs/publisher.py) and again by each sweep below.
  * sweep_due_metrics() — periodic scan (metrics_poll_loop management
    command) that finds published targets due for another poll and enqueues
    them. Polling tapers off and stops entirely after METRICS_POLL_WINDOW_DAYS
    since most engagement happens in a post's first days.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from connectors.base import ChannelCredentials
from connectors.errors import ConnectorError
from connectors.registry import get as get_connector
from django.db.models import Max, Q
from django.utils import timezone
from omnipost_api.models import PostMetric, PostTarget

from . import queue as job_queue

logger = logging.getLogger(__name__)

# How long after publishing a post is still worth polling for engagement.
METRICS_POLL_WINDOW_DAYS = 30
# Minimum spacing between polls of the same target, once it has at least one
# metric snapshot — keeps a 30-day window from hammering rate-limited APIs.
METRICS_POLL_INTERVAL_HOURS = 6


def poll_metrics(post_target_id: int) -> None:
    try:
        post_target = PostTarget.objects.select_related("channel").get(pk=post_target_id)
    except PostTarget.DoesNotExist:
        return

    if post_target.status != PostTarget.STATUS_PUBLISHED or not post_target.remote_id:
        return

    channel = post_target.channel
    connector = get_connector(channel.connector_slug)
    credentials = ChannelCredentials(values=channel.get_credentials(), external_account_id=channel.external_account_id)

    try:
        raw = connector.fetch_metrics(credentials, post_target.remote_id)
    except ConnectorError as exc:
        logger.info(
            "Metrics fetch skipped for post_target %s (%s): %s", post_target_id, exc.error_class.value, exc.safe_detail
        )
        return
    except Exception:  # noqa: BLE001 - best-effort; never let a bad connector break the sweep
        logger.exception("Unexpected error fetching metrics for post_target %s", post_target_id)
        return

    if not raw:
        return  # connector doesn't support metrics for this platform yet

    PostMetric.objects.create(
        post_target=post_target,
        impressions=raw.get("impressions"),
        likes=raw.get("likes"),
        comments=raw.get("comments"),
        shares=raw.get("shares"),
        raw=raw,
    )


def sweep_due_metrics() -> int:
    now = timezone.now()
    window_start = now - timedelta(days=METRICS_POLL_WINDOW_DAYS)
    poll_cutoff = now - timedelta(hours=METRICS_POLL_INTERVAL_HOURS)

    candidates = (
        PostTarget.objects.filter(
            status=PostTarget.STATUS_PUBLISHED,
            published_at__gte=window_start,
        )
        .exclude(remote_id="")
        .annotate(last_polled=Max("metrics__fetched_at"))
        .filter(Q(last_polled__isnull=True) | Q(last_polled__lt=poll_cutoff))
        .values_list("pk", flat=True)
    )

    count = 0
    for post_target_id in candidates:
        job_queue.enqueue_metrics_poll(post_target_id)
        count += 1
    return count
