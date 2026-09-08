"""Prometheus /metrics exporter (Phase 7).

Every gauge here is computed fresh from Postgres (and RQ's Redis) at scrape
time rather than accumulated in-process with prometheus_client's default
Counter objects. This app runs as several separate processes — web, worker,
reconciler, recurrence, token-refresher, metrics-poller — each its own
Python interpreter, so an in-process counter incremented inside the worker
would never be visible to a scrape hitting the web process. Postgres is
already this project's source of truth (see jobs/reconciler.py's docstring);
reading it fresh on every scrape sidesteps prometheus_client's multiprocess
mode entirely, which needs a shared directory on disk and doesn't fit a
multi-container deployment cleanly.
"""

from __future__ import annotations

import logging

import django_rq
import redis
from django.conf import settings
from django.db.models import Count, Sum
from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_GET
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest

from .models import Channel, PublishAttempt, UsageCounter

logger = logging.getLogger(__name__)

# Plain Django views, not DRF APIViews, so REST_FRAMEWORK's
# DEFAULT_PERMISSION_CLASSES never applies — a scrape target can't carry a
# per-user auth token, so it's gated by METRICS_AUTH_TOKEN instead (see
# settings.py), the same shared-secret pattern a Prometheus scrape config
# already expects via bearer_token_file.


@require_GET
def metrics_view(request: HttpRequest) -> HttpResponse:
    token = settings.METRICS_AUTH_TOKEN
    if token:
        provided = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if provided != token and request.GET.get("token") != token:
            return HttpResponse("Forbidden", status=403)

    registry = CollectorRegistry()

    publish_attempts = Gauge(
        "omnipost_publish_attempts_total",
        "PublishAttempt rows by connector and terminal status (all-time snapshot).",
        ["connector_slug", "status"],
        registry=registry,
    )
    for row in (
        PublishAttempt.objects.filter(status__in=[PublishAttempt.STATUS_SUCCEEDED, PublishAttempt.STATUS_FAILED])
        .values("post_target__channel__connector_slug", "status")
        .annotate(count=Count("id"))
    ):
        publish_attempts.labels(
            connector_slug=row["post_target__channel__connector_slug"], status=row["status"]
        ).set(row["count"])

    queue_depth = Gauge("omnipost_queue_depth", "Pending jobs per RQ queue.", ["queue"], registry=registry)
    try:
        for queue_name in settings.RQ_QUEUES:
            queue_depth.labels(queue=queue_name).set(django_rq.get_queue(queue_name).count)
    except redis.RedisError:
        # Redis being unreachable shouldn't 500 the whole scrape — the other
        # gauges below read Postgres only and are still worth reporting.
        logger.warning("Could not read RQ queue depths for /metrics: Redis unreachable")

    channel_health = Gauge(
        "omnipost_channel_health",
        "Connected channels by connector and Connection Health status.",
        ["connector_slug", "health"],
        registry=registry,
    )
    for row in Channel.objects.values("connector_slug", "health").annotate(count=Count("id")):
        channel_health.labels(connector_slug=row["connector_slug"], health=row["health"]).set(row["count"])

    ai_usage = Gauge(
        "omnipost_ai_usage_committed",
        "Committed AI generations by feature, all workspaces, all time. Call counts, not "
        "dollar spend -- a per-call cost ledger was deferred (see Phase 4 project memory).",
        ["feature"],
        registry=registry,
    )
    for row in UsageCounter.objects.values("feature").annotate(total=Sum("committed")):
        ai_usage.labels(feature=row["feature"]).set(row["total"] or 0)

    return HttpResponse(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)
