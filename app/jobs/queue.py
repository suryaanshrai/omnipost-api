"""Thin wrapper around django-rq so the rest of the job engine doesn't import
RQ directly — makes it possible to swap the transport later without touching
publisher.py/scheduler.py/reconciler.py, and gives tests one seam to patch.

Redis here is transport only, never the source of truth: every PublishAttempt
this enqueues also exists as a row in Postgres with its own run_at, which is
what the reconciler (reconciler.py) uses to recover anything lost if Redis is
flushed or a job is dropped.
"""

from __future__ import annotations

from datetime import datetime

import django_rq


def enqueue_attempt_at(attempt_id: int, run_at: datetime) -> None:
    queue = django_rq.get_queue("publish")
    queue.enqueue_at(run_at, "jobs.publisher.run_publish_attempt", attempt_id)


def enqueue_attempt_now(attempt_id: int) -> None:
    queue = django_rq.get_queue("publish")
    queue.enqueue("jobs.publisher.run_publish_attempt", attempt_id)


def enqueue_preflight_at(post_target_id: int, run_at: datetime) -> None:
    queue = django_rq.get_queue("maintenance")
    queue.enqueue_at(run_at, "jobs.health.run_preflight_for_target", post_target_id)


def enqueue_probe(asset_id: int) -> None:
    queue = django_rq.get_queue("media")
    queue.enqueue("jobs.media.probe_asset", asset_id)


def enqueue_rendition(asset_id: int, profile: str) -> None:
    queue = django_rq.get_queue("media")
    queue.enqueue("jobs.media.generate_rendition", asset_id, profile)


def enqueue_metrics_poll(post_target_id: int) -> None:
    queue = django_rq.get_queue("metrics")
    queue.enqueue("jobs.metrics.poll_metrics", post_target_id)
