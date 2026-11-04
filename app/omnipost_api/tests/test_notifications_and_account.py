from unittest import mock

import responses
from django.core import mail
from django.test import override_settings
from jobs.notify import send_notification_email
from jobs.publisher import run_publish_attempt
from jobs.scheduler import schedule_post_target
from rest_framework.test import APIClient

from omnipost_api.models import Channel, Membership, Notification, Post, PostTarget, Workspace


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _setup(django_user_model, username="nina"):
    user = django_user_model.objects.create_user(username=username, email=f"{username}@example.com", password="pw")
    workspace = user.memberships.get().workspace
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="Toots")
    channel.set_credentials({"ACCESS_TOKEN": "t", "INSTANCE_DOMAIN": "mastodon.social"})
    return user, workspace, channel


@responses.activate
def test_a_failed_post_notifies_the_owner_and_queues_an_email(db, django_user_model, enqueued, monkeypatch):
    user, workspace, channel = _setup(django_user_model)
    emails = []
    monkeypatch.setattr("jobs.queue.enqueue_notification_email", emails.append)
    post = Post.objects.create(workspace=workspace, author=user, kind="text", base_text="hello")
    target = PostTarget.objects.create(post=post, channel=channel)
    responses.add(responses.POST, "https://mastodon.social/api/v1/statuses", status=422, json={"error": "no"})

    with mock.patch("django.db.transaction.on_commit", side_effect=lambda fn: fn()):
        run_publish_attempt(schedule_post_target(target).pk)

    note = Notification.objects.get(user=user)
    assert note.kind == Notification.KIND_POST_FAILED
    assert "Toots" in note.title
    assert emails == [note.pk]


@responses.activate
def test_a_broken_channel_notifies_once(db, django_user_model, enqueued):
    user, workspace, channel = _setup(django_user_model, "omar")
    responses.add(responses.POST, "https://mastodon.social/api/v1/statuses", status=401, json={})
    for _ in range(2):
        post = Post.objects.create(workspace=workspace, author=user, kind="text", base_text="x")
        run_publish_attempt(schedule_post_target(PostTarget.objects.create(post=post, channel=channel)).pk)
    assert Notification.objects.filter(user=user, kind=Notification.KIND_CHANNEL_BROKEN).count() == 1


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", FRONTEND_URL="https://app.x")
def test_notification_email_respects_the_preference(db, django_user_model):
    user, workspace, _ = _setup(django_user_model, "pat")
    note = Notification.objects.create(user=user, workspace=workspace, kind="post_failed", title="T", link="/app")
    send_notification_email(note.pk)
    send_notification_email(note.pk)  # already sent: no second email
    assert len(mail.outbox) == 1
    assert "https://app.x/app" in mail.outbox[0].body

    user.email_notifications = False
    user.save()
    other = Notification.objects.create(user=user, workspace=workspace, kind="post_failed", title="T2")
    send_notification_email(other.pk)
    assert len(mail.outbox) == 1


def test_notification_list_is_private_and_can_be_marked_read(db, django_user_model):
    user, workspace, _ = _setup(django_user_model, "quin")
    stranger, _, _ = _setup(django_user_model, "rae")
    note = Notification.objects.create(user=user, workspace=workspace, kind="post_failed", title="T")

    assert _client_for(stranger).get("/notifications/").data["count"] == 0
    client = _client_for(user)
    assert client.get("/notifications/unread-count/").data == {"unread": 1}
    assert client.post(f"/notifications/{note.pk}/read/").status_code == 200
    assert client.get("/notifications/?unread=1").data["count"] == 0
    assert _client_for(stranger).post(f"/notifications/{note.pk}/read/").status_code == 404


def test_new_credentials_reconnect_a_broken_channel(db, django_user_model):
    user, _, channel = _setup(django_user_model, "sam")
    Channel.objects.filter(pk=channel.pk).update(health=Channel.HEALTH_BROKEN, health_detail="expired")
    response = _client_for(user).patch(
        f"/channels/{channel.pk}/",
        {"credentials": {"ACCESS_TOKEN": "new", "INSTANCE_DOMAIN": "mastodon.social"}},
        format="json",
    )
    assert response.status_code == 200, response.data
    channel.refresh_from_db()
    assert channel.health == Channel.HEALTH_HEALTHY
    assert channel.get_credentials()["ACCESS_TOKEN"] == "new"


def test_export_has_posts_and_no_secrets(db, django_user_model):
    user, workspace, _ = _setup(django_user_model, "tess")
    Post.objects.create(workspace=workspace, author=user, kind="text", base_text="my post")
    response = _client_for(user).get("/account/export/")
    assert response.status_code == 200
    body = str(response.data)
    assert "my post" in body
    assert "ACCESS_TOKEN" not in body


def test_delete_account_removes_solo_workspaces(db, django_user_model):
    user, workspace, channel = _setup(django_user_model, "uma2")
    client = _client_for(user)
    assert client.post("/account/delete/", {"password": "wrong"}, format="json").status_code == 400
    assert client.post("/account/delete/", {"password": "pw"}, format="json").status_code == 204
    assert not django_user_model.objects.filter(pk=user.pk).exists()
    assert not Workspace.objects.filter(pk=workspace.pk).exists()
    assert not Channel.objects.filter(pk=channel.pk).exists()


def test_delete_account_refuses_to_orphan_a_shared_workspace(db, django_user_model):
    user, workspace, _ = _setup(django_user_model, "vik")
    other = django_user_model.objects.create_user(username="wes", password="x")
    Membership.objects.create(user=other, workspace=workspace, role=Membership.ROLE_EDITOR)
    response = _client_for(user).post("/account/delete/", {"password": "pw"}, format="json")
    assert response.status_code == 400
    assert django_user_model.objects.filter(pk=user.pk).exists()


def test_user_can_turn_off_email_alerts(db, django_user_model):
    user, _, _ = _setup(django_user_model, "xan")
    client = _client_for(user)
    assert client.get("/auth/user/").data["email_notifications"] is True
    assert client.patch("/auth/user/", {"email_notifications": False}, format="json").status_code == 200
    user.refresh_from_db()
    assert user.email_notifications is False
