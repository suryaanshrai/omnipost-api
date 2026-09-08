from datetime import timedelta

from django.utils import timezone
from omnipost_api.models import Post, PostTarget, RecurrenceRule

from jobs.recurrence import run_due_recurrences


def test_due_rule_creates_and_schedules_a_post_with_next_variant(mastodon_channel, workspace, enqueued):
    rule = RecurrenceRule.objects.create(
        workspace=workspace,
        channel=mastodon_channel,
        variants=["first", "second", "third"],
        interval_hours=24,
        next_run_at=timezone.now() - timedelta(minutes=1),
    )

    fired = run_due_recurrences()
    assert fired == 1

    post = Post.objects.get(base_text="first")
    assert post.status == Post.STATUS_SCHEDULED
    target = PostTarget.objects.get(post=post)
    assert target.channel_id == mastodon_channel.pk
    assert ("at", target.attempts.get().pk) in enqueued

    rule.refresh_from_db()
    assert rule.next_variant_index == 1
    assert rule.next_run_at > timezone.now() + timedelta(hours=23)


def test_variants_rotate_round_robin_across_multiple_fires(mastodon_channel, workspace, enqueued):
    rule = RecurrenceRule.objects.create(
        workspace=workspace,
        channel=mastodon_channel,
        variants=["a", "b"],
        interval_hours=1,
        next_run_at=timezone.now() - timedelta(hours=3),
    )
    run_due_recurrences()
    rule.next_run_at = timezone.now() - timedelta(minutes=1)
    rule.save(update_fields=["next_run_at"])
    run_due_recurrences()

    texts = sorted(Post.objects.filter(workspace=workspace).values_list("base_text", flat=True))
    assert texts == ["a", "b"]


def test_inactive_rule_is_not_fired(mastodon_channel, workspace, enqueued):
    RecurrenceRule.objects.create(
        workspace=workspace,
        channel=mastodon_channel,
        variants=["x"],
        interval_hours=1,
        next_run_at=timezone.now() - timedelta(minutes=1),
        is_active=False,
    )
    assert run_due_recurrences() == 0
    assert not Post.objects.exists()


def test_rule_past_end_at_deactivates_without_firing(mastodon_channel, workspace, enqueued):
    rule = RecurrenceRule.objects.create(
        workspace=workspace,
        channel=mastodon_channel,
        variants=["x"],
        interval_hours=1,
        next_run_at=timezone.now() - timedelta(minutes=1),
        end_at=timezone.now() - timedelta(days=1),
    )
    assert run_due_recurrences() == 0
    rule.refresh_from_db()
    assert rule.is_active is False
    assert not Post.objects.exists()


def test_rule_not_yet_due_is_untouched(mastodon_channel, workspace, enqueued):
    RecurrenceRule.objects.create(
        workspace=workspace,
        channel=mastodon_channel,
        variants=["x"],
        interval_hours=1,
        next_run_at=timezone.now() + timedelta(hours=1),
    )
    assert run_due_recurrences() == 0
