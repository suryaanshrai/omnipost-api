from datetime import UTC, datetime

import pytest

from omnipost_api.models import BlackoutWindow, Post, PostTarget, QueueSlot
from omnipost_api.services.queueing import NoOpenSlotError, next_open_slot_run_at

# A fixed Monday so every test's "next Tuesday" etc. is deterministic instead
# of depending on when the suite happens to run.
_MONDAY = datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def test_no_active_slots_raises(mastodon_channel):
    with pytest.raises(NoOpenSlotError):
        next_open_slot_run_at(mastodon_channel, after=_MONDAY)


def test_resolves_next_occurrence_in_channel_local_time(mastodon_channel):
    mastodon_channel.timezone = "America/New_York"
    mastodon_channel.save()
    QueueSlot.objects.create(channel=mastodon_channel, weekday=1, time_of_day="09:00")  # Tuesday

    run_at = next_open_slot_run_at(mastodon_channel, after=_MONDAY)

    # 2024-01-02 09:00 EST (UTC-5, no DST in January) == 14:00 UTC.
    assert run_at == datetime(2024, 1, 2, 14, 0, tzinfo=UTC)


def test_falls_back_to_workspace_timezone_when_channel_has_none(mastodon_channel):
    mastodon_channel.workspace.timezone = "America/New_York"
    mastodon_channel.workspace.save()
    QueueSlot.objects.create(channel=mastodon_channel, weekday=1, time_of_day="09:00")

    run_at = next_open_slot_run_at(mastodon_channel, after=_MONDAY)
    assert run_at == datetime(2024, 1, 2, 14, 0, tzinfo=UTC)


def test_skips_a_slot_that_is_already_occupied(workspace, mastodon_channel):
    mastodon_channel.timezone = "UTC"
    mastodon_channel.save()
    QueueSlot.objects.create(channel=mastodon_channel, weekday=1, time_of_day="09:00")

    occupied_at = datetime(2024, 1, 2, 9, 0, tzinfo=UTC)
    post = Post.objects.create(workspace=workspace, kind="text", base_text="already booked")
    PostTarget.objects.create(
        post=post, channel=mastodon_channel, status=PostTarget.STATUS_SCHEDULED, run_at=occupied_at
    )

    run_at = next_open_slot_run_at(mastodon_channel, after=_MONDAY)
    # Only weekly slot is Tuesday 09:00 UTC — the next open one is a week later.
    assert run_at == datetime(2024, 1, 9, 9, 0, tzinfo=UTC)


def test_canceled_targets_do_not_occupy_their_slot(workspace, mastodon_channel):
    mastodon_channel.timezone = "UTC"
    mastodon_channel.save()
    QueueSlot.objects.create(channel=mastodon_channel, weekday=1, time_of_day="09:00")

    occupied_at = datetime(2024, 1, 2, 9, 0, tzinfo=UTC)
    post = Post.objects.create(workspace=workspace, kind="text", base_text="canceled")
    PostTarget.objects.create(
        post=post, channel=mastodon_channel, status=PostTarget.STATUS_CANCELED, run_at=occupied_at
    )

    run_at = next_open_slot_run_at(mastodon_channel, after=_MONDAY)
    assert run_at == occupied_at


def test_min_gap_skips_a_slot_too_close_to_an_occupied_one(workspace, mastodon_channel):
    mastodon_channel.timezone = "UTC"
    mastodon_channel.min_gap_minutes = 120
    mastodon_channel.save()
    QueueSlot.objects.create(channel=mastodon_channel, weekday=1, time_of_day="09:00")
    QueueSlot.objects.create(channel=mastodon_channel, weekday=1, time_of_day="10:00")

    occupied_at = datetime(2024, 1, 2, 9, 0, tzinfo=UTC)
    post = Post.objects.create(workspace=workspace, kind="text", base_text="booked")
    PostTarget.objects.create(
        post=post, channel=mastodon_channel, status=PostTarget.STATUS_SCHEDULED, run_at=occupied_at
    )

    run_at = next_open_slot_run_at(mastodon_channel, after=_MONDAY)
    # 10:00 is only 60min from the 09:00 booking (< 120min min_gap) so it's
    # skipped too — the next open slot is next week's 09:00.
    assert run_at == datetime(2024, 1, 9, 9, 0, tzinfo=UTC)


def test_blackout_window_skips_covered_slots(workspace, mastodon_channel):
    mastodon_channel.timezone = "UTC"
    mastodon_channel.save()
    QueueSlot.objects.create(channel=mastodon_channel, weekday=1, time_of_day="09:00")
    BlackoutWindow.objects.create(
        workspace=workspace,
        channel=mastodon_channel,
        starts_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
        ends_at=datetime(2024, 1, 3, 0, 0, tzinfo=UTC),
    )

    run_at = next_open_slot_run_at(mastodon_channel, after=_MONDAY)
    assert run_at == datetime(2024, 1, 9, 9, 0, tzinfo=UTC)


def test_workspace_wide_blackout_window_applies_with_no_channel_set(workspace, mastodon_channel):
    mastodon_channel.timezone = "UTC"
    mastodon_channel.save()
    QueueSlot.objects.create(channel=mastodon_channel, weekday=1, time_of_day="09:00")
    BlackoutWindow.objects.create(
        workspace=workspace,
        channel=None,
        starts_at=datetime(2024, 1, 2, 0, 0, tzinfo=UTC),
        ends_at=datetime(2024, 1, 3, 0, 0, tzinfo=UTC),
    )

    run_at = next_open_slot_run_at(mastodon_channel, after=_MONDAY)
    assert run_at == datetime(2024, 1, 9, 9, 0, tzinfo=UTC)
