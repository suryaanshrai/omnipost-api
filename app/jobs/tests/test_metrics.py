from datetime import timedelta

import responses
from django.utils import timezone
from omnipost_api.models import PostMetric, PostTarget

from jobs.metrics import poll_metrics, sweep_due_metrics


def _publish(post_target, remote_id="999"):
    post_target.status = PostTarget.STATUS_PUBLISHED
    post_target.remote_id = remote_id
    post_target.published_at = timezone.now()
    post_target.save()


def test_poll_metrics_skips_unpublished_target(post_target):
    poll_metrics(post_target.pk)
    assert PostMetric.objects.count() == 0


def test_poll_metrics_skips_target_without_remote_id(post_target):
    post_target.status = PostTarget.STATUS_PUBLISHED
    post_target.save()
    poll_metrics(post_target.pk)
    assert PostMetric.objects.count() == 0


@responses.activate
def test_poll_metrics_creates_snapshot(post_target):
    _publish(post_target)
    responses.add(
        responses.GET,
        "https://mastodon.social/api/v1/statuses/999",
        json={"favourites_count": 5, "reblogs_count": 1, "replies_count": 2},
        status=200,
    )
    poll_metrics(post_target.pk)
    metric = PostMetric.objects.get(post_target=post_target)
    assert metric.likes == 5
    assert metric.shares == 1
    assert metric.comments == 2
    assert metric.raw["likes"] == 5


@responses.activate
def test_poll_metrics_swallows_connector_error(post_target):
    _publish(post_target)
    responses.add(responses.GET, "https://mastodon.social/api/v1/statuses/999", json={}, status=401)
    poll_metrics(post_target.pk)  # must not raise, and must not record anything
    assert PostMetric.objects.count() == 0


def test_sweep_enqueues_target_with_no_metrics_yet(post_target, enqueued):
    _publish(post_target)
    count = sweep_due_metrics()
    assert count == 1
    assert ("metrics_poll", post_target.pk) in enqueued


def test_sweep_skips_recently_polled_target(post_target, enqueued):
    _publish(post_target)
    PostMetric.objects.create(post_target=post_target, likes=1)
    assert sweep_due_metrics() == 0


def test_sweep_skips_target_outside_poll_window(post_target, enqueued):
    _publish(post_target)
    post_target.published_at = timezone.now() - timedelta(days=60)
    post_target.save()
    assert sweep_due_metrics() == 0


def test_sweep_ignores_unpublished_target(post_target, enqueued):
    assert sweep_due_metrics() == 0
