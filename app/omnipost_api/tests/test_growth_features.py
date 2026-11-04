"""Repost, batch analytics, account-wide AI quota, link-in-bio, first comment,
CSV row atomicity and recurrence catch-up."""

from datetime import timedelta

import pytest
import responses
from django.utils import timezone
from rest_framework.test import APIClient

from omnipost_api.models import (
    Channel,
    Membership,
    Post,
    PostMetric,
    PostTarget,
    QueueSlot,
    RecurrenceRule,
    UsageCounter,
    Workspace,
)


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _setup(django_user_model, name):
    user = django_user_model.objects.create_user(username=name, email=f"{name}@example.com", password="x")
    workspace = user.memberships.get().workspace
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="M")
    channel.set_credentials({"ACCESS_TOKEN": "t", "INSTANCE_DOMAIN": "mastodon.social"})
    return user, workspace, channel


def test_repost_copies_and_queues(db, django_user_model, enqueued):
    user, workspace, channel = _setup(django_user_model, "rep")
    for weekday in range(7):
        QueueSlot.objects.create(channel=channel, weekday=weekday, time_of_day="09:00")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="Evergreen tip", status="published")
    PostTarget.objects.create(post=post, channel=channel, status="published", remote_id="1")

    response = _client_for(user).post(f"/posts/{post.pk}/repost/", {}, format="json")
    assert response.status_code == 201, response.data
    copy = Post.objects.get(pk=response.data["id"])
    assert copy.pk != post.pk and copy.status == "scheduled" and copy.base_text == "Evergreen tip"
    assert copy.scheduled_for > timezone.now()


def test_repost_without_queue_slots_says_why(db, django_user_model, enqueued):
    user, workspace, channel = _setup(django_user_model, "rep2")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="x", status="published")
    PostTarget.objects.create(post=post, channel=channel, status="published")
    response = _client_for(user).post(f"/posts/{post.pk}/repost/", {}, format="json")
    assert response.status_code == 400
    assert Post.objects.count() == 1  # the copy was cleaned up


def test_batch_performance(db, django_user_model):
    user, workspace, channel = _setup(django_user_model, "perf")
    a = Post.objects.create(workspace=workspace, kind="text", base_text="a", status="published")
    b = Post.objects.create(workspace=workspace, kind="text", base_text="b", status="published")
    ta = PostTarget.objects.create(post=a, channel=channel, status="published", remote_id="1")
    PostTarget.objects.create(post=b, channel=channel, status="published", remote_id="2")
    PostMetric.objects.create(post_target=ta, likes=5, raw={})
    data = _client_for(user).get(f"/posts/performance/?workspace={workspace.pk}&ids={a.pk},{b.pk}").data
    assert data["results"][str(a.pk)][0]["metric"]["likes"] == 5
    assert data["results"][str(b.pk)] == []
    stranger, _, _ = _setup(django_user_model, "perf2")
    assert _client_for(stranger).get(f"/posts/performance/?workspace={workspace.pk}&ids={a.pk}").status_code == 403


def test_free_ai_quota_is_shared_across_an_owners_workspaces(db, django_user_model, settings):
    from ai import quota
    from ai.errors import QuotaExceeded

    settings.AI_FREE_TEXT_LIMIT = 1
    user, first, _ = _setup(django_user_model, "quota")
    response = _client_for(user).post("/workspaces/", {"name": "Second"}, format="json")
    second = Workspace.objects.get(pk=response.data["id"])

    quota.commit(quota.reserve(first, UsageCounter.FEATURE_AI_TEXT))
    with pytest.raises(QuotaExceeded):
        quota.reserve(second, UsageCounter.FEATURE_AI_TEXT)


def test_bio_page_is_public_only_when_switched_on(db, django_user_model):
    user, workspace, channel = _setup(django_user_model, "bio")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="Read this", status="published")
    PostTarget.objects.create(post=post, channel=channel, status="published", permalink="https://m.social/@me/1")
    anon = APIClient()
    assert anon.get(f"/public/bio/{workspace.slug}/").status_code == 404

    owner = _client_for(user)
    bad = owner.patch(
        f"/workspaces/{workspace.pk}/", {"bio_links": [{"label": "x", "url": "javascript:1"}]}, format="json"
    )
    assert bad.status_code == 400
    owner.patch(
        f"/workspaces/{workspace.pk}/",
        {"bio_enabled": True, "bio_description": "Hi", "bio_links": [{"label": "Shop", "url": "https://shop.ex"}]},
        format="json",
    )
    page = anon.get(f"/public/bio/{workspace.slug}/").data
    assert page["links"] == [{"label": "Shop", "url": "https://shop.ex"}]
    assert page["posts"][0]["url"] == "https://m.social/@me/1"


@responses.activate
def test_first_comment_is_posted_after_publishing(db, django_user_model, enqueued):
    from jobs.first_comment import post_first_comment
    from jobs.publisher import run_publish_attempt
    from jobs.scheduler import schedule_post_target

    user, workspace, channel = _setup(django_user_model, "fc")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="Main post")
    target = PostTarget.objects.create(post=post, channel=channel, first_comment="Link: https://ex.com")
    responses.add(responses.POST, "https://mastodon.social/api/v1/statuses", json={"id": "10", "url": "u"})
    responses.add(responses.POST, "https://mastodon.social/api/v1/statuses", json={"id": "11", "url": "u2"})
    run_publish_attempt(schedule_post_target(target).pk)
    assert ("first_comment", target.pk) in enqueued

    post_first_comment(target.pk)
    import json

    reply = json.loads(responses.calls[1].request.body)
    assert reply == {"status": "Link: https://ex.com", "in_reply_to_id": "10"}
    target.refresh_from_db()
    assert target.first_comment_result == "posted"


def test_csv_rows_are_all_or_nothing(db, django_user_model, enqueued):
    user, workspace, channel = _setup(django_user_model, "csv")
    csv_text = (
        "text,channel_id,kind,run_at\n"
        f"fine,{channel.pk},text,2030-01-01T09:00:00Z\n"
        f"bad kind,{channel.pk},podcast,2030-01-01T09:00:00Z\n"
        f"no slot,{channel.pk},text,\n"
    )
    response = _client_for(user).post(
        "/posts/import-csv/", {"workspace": workspace.pk, "csv_text": csv_text}, format="multipart"
    )
    assert response.data["created"] == 1
    assert [e["row"] for e in response.data["errors"]] == [3, 4]
    # The failed rows left no half-made posts behind.
    assert Post.objects.filter(workspace=workspace).count() == 1


def test_recurrence_after_downtime_posts_once_not_a_burst(db, django_user_model, enqueued):
    from jobs.recurrence import run_due_recurrences

    user, workspace, channel = _setup(django_user_model, "rec")
    rule = RecurrenceRule.objects.create(
        workspace=workspace,
        channel=channel,
        kind="text",
        variants=["a", "b"],
        interval_hours=1,
        next_run_at=timezone.now() - timedelta(hours=10),
    )
    assert run_due_recurrences() == 1
    assert run_due_recurrences() == 0
    rule.refresh_from_db()
    assert rule.next_run_at > timezone.now()
    assert Membership.objects.filter(workspace=workspace).exists()
