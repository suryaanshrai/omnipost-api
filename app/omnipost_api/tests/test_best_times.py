from datetime import UTC, datetime

from omnipost_api.models import Post, PostMetric, PostTarget
from omnipost_api.services.best_times import best_times_for_channel


def _published_target(workspace, channel, when: datetime, engagement: int) -> PostTarget:
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    target = PostTarget.objects.create(
        post=post, channel=channel, status=PostTarget.STATUS_PUBLISHED, remote_id="x", published_at=when
    )
    PostMetric.objects.create(post_target=target, likes=engagement)
    return target


def test_returns_empty_below_minimum_sample_size(workspace, mastodon_channel):
    # Monday 09:00 UTC, four times — one short of MIN_TOTAL_SAMPLES (5)
    for _ in range(4):
        _published_target(workspace, mastodon_channel, datetime(2026, 1, 5, 9, 0, tzinfo=UTC), 10)
    assert best_times_for_channel(mastodon_channel) == []


def test_ranks_buckets_by_average_engagement(workspace, mastodon_channel):
    # Monday 09:00 UTC: two posts averaging 20 engagement.
    _published_target(workspace, mastodon_channel, datetime(2026, 1, 5, 9, 0, tzinfo=UTC), 10)
    _published_target(workspace, mastodon_channel, datetime(2026, 1, 12, 9, 0, tzinfo=UTC), 30)
    # Wednesday 18:00 UTC: three posts averaging 5 engagement.
    for _ in range(3):
        _published_target(workspace, mastodon_channel, datetime(2026, 1, 7, 18, 0, tzinfo=UTC), 5)

    suggestions = best_times_for_channel(mastodon_channel)

    assert suggestions[0].weekday == 0  # Monday
    assert suggestions[0].hour == 9
    assert suggestions[0].sample_size == 2
    assert suggestions[0].avg_engagement == 20
    assert suggestions[1].weekday == 2  # Wednesday
    assert suggestions[1].hour == 18


def test_ignores_targets_with_no_metrics(workspace, mastodon_channel):
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    for _ in range(5):
        PostTarget.objects.create(
            post=post,
            channel=mastodon_channel,
            status=PostTarget.STATUS_PUBLISHED,
            remote_id="x",
            published_at=datetime(2026, 1, 5, 9, 0, tzinfo=UTC),
        )
    assert best_times_for_channel(mastodon_channel) == []


def test_bucket_below_min_samples_per_bucket_is_excluded(workspace, mastodon_channel):
    # Five posts total (meets MIN_TOTAL_SAMPLES) but scattered across five
    # different single-sample buckets — none meets MIN_SAMPLES_PER_BUCKET (2).
    for day in range(1, 6):
        _published_target(workspace, mastodon_channel, datetime(2026, 1, day, 9, 0, tzinfo=UTC), 10)
    assert best_times_for_channel(mastodon_channel) == []
