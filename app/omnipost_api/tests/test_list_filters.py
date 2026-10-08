"""The per-workspace/post/channel list filters the editorial frontend reads
through: every WorkspaceScopedViewSet honours `?workspace=`, publish
attempts filter by `?post=`/`?post_target=`, queue slots by `?channel=`,
post targets by `?post=` — always narrowing *within* the requesting user's
own memberships, never widening past them."""

from datetime import UTC, datetime, time

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omnipost_api.models import (
    BlackoutWindow,
    Channel,
    Membership,
    Organization,
    Post,
    PostTarget,
    PublishAttempt,
    QueueSlot,
    Workspace,
)


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _second_workspace(user, name="Second"):
    org = Organization.objects.create(name=name)
    workspace = Workspace.objects.create(organization=org, name=name, slug=name.lower())
    Membership.objects.create(workspace=workspace, user=user, role=Membership.ROLE_OWNER)
    return workspace


def _channel(workspace, name):
    return Channel.objects.create(workspace=workspace, connector_slug="discord", display_name=name)


@pytest.fixture
def dana(db, django_user_model):
    return django_user_model.objects.create_user(username="dana", password="x")


def test_channels_workspace_filter_narrows_to_that_workspace(dana):
    first = dana.memberships.get().workspace
    second = _second_workspace(dana)
    a = _channel(first, "A")
    b = _channel(second, "B")
    client = _client_for(dana)

    assert {r["id"] for r in client.get("/channels/").data["results"]} == {a.pk, b.pk}
    assert [r["id"] for r in client.get("/channels/", {"workspace": first.pk}).data["results"]] == [a.pk]
    assert [r["id"] for r in client.get("/channels/", {"workspace": second.pk}).data["results"]] == [b.pk]


def test_workspace_filter_never_reaches_another_users_workspace(dana, django_user_model):
    other = django_user_model.objects.create_user(username="other", password="x")
    others_workspace = other.memberships.get().workspace
    _channel(others_workspace, "Not yours")

    response = _client_for(dana).get("/channels/", {"workspace": others_workspace.pk})

    assert response.status_code == 200
    assert response.data["count"] == 0


def test_workspace_filter_rejects_a_non_integer(dana):
    response = _client_for(dana).get("/channels/", {"workspace": "abc"})
    assert response.status_code == 400
    assert "workspace" in response.data


def test_workspace_filter_follows_workspace_field_on_queue_slots(dana):
    first = dana.memberships.get().workspace
    second = _second_workspace(dana)
    slot_a = QueueSlot.objects.create(channel=_channel(first, "A"), weekday=0, time_of_day=time(9, 0))
    QueueSlot.objects.create(channel=_channel(second, "B"), weekday=1, time_of_day=time(10, 0))

    response = _client_for(dana).get("/queue-slots/", {"workspace": first.pk})

    assert [r["id"] for r in response.data["results"]] == [slot_a.pk]


def test_queue_slots_channel_filter(dana):
    workspace = dana.memberships.get().workspace
    a, b = _channel(workspace, "A"), _channel(workspace, "B")
    slot_a = QueueSlot.objects.create(channel=a, weekday=0, time_of_day=time(9, 0))
    QueueSlot.objects.create(channel=b, weekday=0, time_of_day=time(9, 0))

    response = _client_for(dana).get("/queue-slots/", {"channel": a.pk})

    assert [r["id"] for r in response.data["results"]] == [slot_a.pk]


def test_blackout_windows_workspace_filter(dana):
    first = dana.memberships.get().workspace
    second = _second_workspace(dana)
    now = timezone.now()
    keep = BlackoutWindow.objects.create(workspace=first, starts_at=now, ends_at=now)
    BlackoutWindow.objects.create(workspace=second, starts_at=now, ends_at=now)

    response = _client_for(dana).get("/blackout-windows/", {"workspace": first.pk})

    assert [r["id"] for r in response.data["results"]] == [keep.pk]


def test_publish_attempts_filter_by_post_and_by_target(dana):
    workspace = dana.memberships.get().workspace
    channel = _channel(workspace, "A")
    post_one = Post.objects.create(workspace=workspace, kind="text", base_text="one")
    post_two = Post.objects.create(workspace=workspace, kind="text", base_text="two")
    t1 = PostTarget.objects.create(post=post_one, channel=channel)
    t2 = PostTarget.objects.create(post=post_one, channel=_channel(workspace, "B"))
    t3 = PostTarget.objects.create(post=post_two, channel=channel)
    run_at = datetime(2026, 1, 1, tzinfo=UTC)
    a1 = PublishAttempt.objects.create(post_target=t1, run_at=run_at)
    a2 = PublishAttempt.objects.create(post_target=t2, run_at=run_at)
    PublishAttempt.objects.create(post_target=t3, run_at=run_at)
    client = _client_for(dana)

    by_post = client.get("/publish-attempts/", {"post": post_one.pk})
    assert {r["id"] for r in by_post.data["results"]} == {a1.pk, a2.pk}

    by_target = client.get("/publish-attempts/", {"post_target": t2.pk})
    assert [r["id"] for r in by_target.data["results"]] == [a2.pk]


def test_publish_attempts_post_filter_stays_scoped_to_the_user(dana, django_user_model):
    other = django_user_model.objects.create_user(username="other", password="x")
    others_workspace = other.memberships.get().workspace
    post = Post.objects.create(workspace=others_workspace, kind="text", base_text="theirs")
    target = PostTarget.objects.create(post=post, channel=_channel(others_workspace, "Theirs"))
    PublishAttempt.objects.create(post_target=target, run_at=timezone.now())

    response = _client_for(dana).get("/publish-attempts/", {"post": post.pk})

    assert response.status_code == 200
    assert response.data["count"] == 0


def test_post_targets_post_filter(dana):
    workspace = dana.memberships.get().workspace
    channel = _channel(workspace, "A")
    post_one = Post.objects.create(workspace=workspace, kind="text", base_text="one")
    post_two = Post.objects.create(workspace=workspace, kind="text", base_text="two")
    keep = PostTarget.objects.create(post=post_one, channel=channel)
    PostTarget.objects.create(post=post_two, channel=channel)

    response = _client_for(dana).get("/post-targets/", {"post": post_one.pk})

    assert [r["id"] for r in response.data["results"]] == [keep.pk]


def test_calendar_entries_carry_their_posts_kind_text_and_status(dana):
    workspace = dana.memberships.get().workspace
    post = Post.objects.create(workspace=workspace, kind="text", base_text="Launch day", status=Post.STATUS_SCHEDULED)
    run_at = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    PostTarget.objects.create(post=post, channel=_channel(workspace, "A"), run_at=run_at)

    response = _client_for(dana).get(
        "/posts/calendar/",
        {"workspace": workspace.pk, "start": "2026-03-02T00:00:00Z", "end": "2026-03-09T00:00:00Z"},
    )

    assert response.status_code == 200
    [entry] = response.data
    assert entry["post"] == post.pk
    assert entry["post_kind"] == "text"
    assert entry["post_text"] == "Launch day"
    assert entry["post_status"] == "scheduled"
