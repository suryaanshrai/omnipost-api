from zoneinfo import ZoneInfo

from rest_framework.test import APIClient

from omnipost_api.models import Channel, PostTarget, QueueSlot


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _channel(workspace, name, timezone, weekday=1, time_of_day="09:00"):
    channel = Channel.objects.create(
        workspace=workspace, connector_slug="mastodon", display_name=name, timezone=timezone
    )
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    QueueSlot.objects.create(channel=channel, weekday=weekday, time_of_day=time_of_day)
    return channel


def test_csv_import_lands_every_row_in_its_channel_local_slot(db, django_user_model, enqueued):
    """The plan's literal Phase 5 success criterion: import posts by CSV into
    a slot queue across channels in different timezones and have every one
    land in the right local slot."""
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace

    ny = _channel(workspace, "NY", "America/New_York")
    london = _channel(workspace, "London", "Europe/London")
    tokyo = _channel(workspace, "Tokyo", "Asia/Tokyo")
    channels = [ny, london, tokyo]

    rows = ["text,channel_id"]
    expected_channel_ids = []
    for i in range(9):  # 3 posts per channel, all through the queue path
        channel = channels[i % 3]
        rows.append(f"post number {i},{channel.pk}")
        expected_channel_ids.append(channel.pk)
    csv_text = "\n".join(rows)

    client = _client_for(user)
    response = client.post(
        "/posts/import-csv/", {"workspace": workspace.pk, "csv_text": csv_text}, format="multipart"
    )
    assert response.status_code == 200, response.data
    assert response.data == {"created": 9, "errors": []}

    targets = list(PostTarget.objects.filter(channel__in=channels).select_related("channel", "post"))
    assert len(targets) == 9
    for target in targets:
        assert target.run_at is not None
        assert target.status == PostTarget.STATUS_SCHEDULED
        # Every resolved run_at must actually fall on that channel's
        # configured local weekday/time-of-day, not just "some UTC time".
        local = target.run_at.astimezone(ZoneInfo(target.channel.effective_timezone()))
        assert local.weekday() == 1
        assert (local.hour, local.minute) == (9, 0)

    # Each channel got exactly 3 distinct, increasing run_at values (queue
    # resolution must not stack multiple posts on the same slot instance).
    for channel in channels:
        channel_run_ats = sorted(t.run_at for t in targets if t.channel_id == channel.pk)
        assert len(channel_run_ats) == 3
        assert len(set(channel_run_ats)) == 3


def test_csv_import_reports_row_errors_without_failing_the_batch(db, django_user_model, enqueued):
    user = django_user_model.objects.create_user(username="erin", password="x")
    workspace = user.memberships.get().workspace
    good_channel = _channel(workspace, "Good", "UTC")

    csv_text = "text,channel_id\n" f"a fine post,{good_channel.pk}\n" "no channel here,999999\n" ",also bad\n"

    client = _client_for(user)
    response = client.post(
        "/posts/import-csv/", {"workspace": workspace.pk, "csv_text": csv_text}, format="multipart"
    )
    assert response.status_code == 200, response.data
    assert response.data["created"] == 1
    assert len(response.data["errors"]) == 2
    assert response.data["errors"][0]["row"] == 3
    assert response.data["errors"][1]["row"] == 4


def test_csv_import_rejects_workspace_the_user_cannot_access(db, django_user_model):
    user = django_user_model.objects.create_user(username="finn", password="x")
    other = django_user_model.objects.create_user(username="gale", password="x")
    other_workspace = other.memberships.get().workspace

    client = _client_for(user)
    response = client.post(
        "/posts/import-csv/",
        {"workspace": other_workspace.pk, "csv_text": "text,channel_id\nhi,1\n"},
        format="multipart",
    )
    assert response.status_code == 403


def test_calendar_returns_targets_in_range(db, django_user_model, enqueued):
    user = django_user_model.objects.create_user(username="hank", password="x")
    workspace = user.memberships.get().workspace
    channel = _channel(workspace, "C", "UTC")
    client = _client_for(user)

    csv_text = f"text,channel_id\nin range,{channel.pk}\n"
    client.post("/posts/import-csv/", {"workspace": workspace.pk, "csv_text": csv_text}, format="multipart")
    target = PostTarget.objects.get(post__base_text="in range")

    start = (target.run_at.replace(hour=0, minute=0, second=0, microsecond=0)).isoformat()
    end = (target.run_at.replace(hour=23, minute=59, second=59, microsecond=0)).isoformat()
    # Pass query params via the dict form (not an f-string) so the test
    # client percent-encodes the "+00:00" UTC offset correctly — a raw "+"
    # in a query string decodes as a space, which would corrupt the ISO
    # timestamp before it ever reaches the view.
    response = client.get("/posts/calendar/", {"workspace": workspace.pk, "start": start, "end": end})
    assert response.status_code == 200, response.data
    assert [t["id"] for t in response.data] == [target.pk]

    far_start = "2099-01-01T00:00:00Z"
    far_end = "2099-01-02T00:00:00Z"
    empty = client.get("/posts/calendar/", {"workspace": workspace.pk, "start": far_start, "end": far_end})
    assert empty.data == []


def test_queue_action_resolves_next_open_slot_per_target(db, django_user_model, enqueued):
    user = django_user_model.objects.create_user(username="ivan", password="x")
    workspace = user.memberships.get().workspace
    channel = _channel(workspace, "Q", "UTC")
    client = _client_for(user)

    response = client.post(
        "/posts/",
        {
            "workspace": workspace.pk,
            "kind": "text",
            "base_text": "queued post",
            "target_specs": [{"channel": channel.pk}],
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    post_id = response.data["id"]

    response = client.post(f"/posts/{post_id}/queue/")
    assert response.status_code == 200, response.data
    assert response.data["status"] == "scheduled"
    target = PostTarget.objects.get(post_id=post_id)
    assert target.run_at is not None


def test_approval_workflow_blocks_schedule_until_approved(db, django_user_model, enqueued):
    user = django_user_model.objects.create_user(username="jill", password="x")
    workspace = user.memberships.get().workspace
    workspace.approval_workflow_enabled = True
    workspace.save()
    channel = _channel(workspace, "AW", "UTC")
    client = _client_for(user)

    response = client.post(
        "/posts/",
        {
            "workspace": workspace.pk,
            "kind": "text",
            "base_text": "needs approval",
            "target_specs": [{"channel": channel.pk}],
        },
        format="json",
    )
    post_id = response.data["id"]

    blocked = client.post(f"/posts/{post_id}/schedule/")
    assert blocked.status_code == 400

    review = client.post(f"/posts/{post_id}/submit-for-review/")
    assert review.status_code == 200
    assert review.data["status"] == "in_review"

    still_blocked = client.post(f"/posts/{post_id}/schedule/")
    assert still_blocked.status_code == 400

    approved = client.post(f"/posts/{post_id}/approve/")
    assert approved.status_code == 200
    assert approved.data["status"] == "approved"

    scheduled = client.post(f"/posts/{post_id}/schedule/")
    assert scheduled.status_code == 200
    assert scheduled.data["status"] == "scheduled"


def test_approval_workflow_off_by_default_allows_direct_schedule(db, django_user_model, enqueued):
    user = django_user_model.objects.create_user(username="kara", password="x")
    workspace = user.memberships.get().workspace
    channel = _channel(workspace, "Direct", "UTC")
    client = _client_for(user)

    response = client.post(
        "/posts/",
        {"workspace": workspace.pk, "kind": "text", "base_text": "direct", "target_specs": [{"channel": channel.pk}]},
        format="json",
    )
    post_id = response.data["id"]

    scheduled = client.post(f"/posts/{post_id}/schedule/")
    assert scheduled.status_code == 200
    assert scheduled.data["status"] == "scheduled"
