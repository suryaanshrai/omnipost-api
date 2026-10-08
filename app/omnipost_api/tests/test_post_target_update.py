"""target_specs was create-only: PostSerializer had no update(), so a draft's
channel targets couldn't change once the Post existed — no endpoint for a
Drafts screen to say "actually, also post this to LinkedIn". These tests
cover the new PATCH behavior: omitting target_specs leaves existing targets
alone, passing it replaces the full set, and it's refused once a post has
moved past draft/failed (replacing targets there would cascade-delete real
PublishAttempt/PostMetric history)."""

from rest_framework.test import APIClient

from omnipost_api.models import Channel, Post, PostTarget


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _channel(workspace, name):
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name=name)
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    return channel


def test_patch_with_target_specs_replaces_targets_on_a_draft(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    a, b = _channel(workspace, "A"), _channel(workspace, "B")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    PostTarget.objects.create(post=post, channel=a)

    response = _client_for(user).patch(
        f"/posts/{post.pk}/", {"target_specs": [{"channel": b.pk}]}, format="json"
    )

    assert response.status_code == 200, response.data
    remaining = list(post.targets.values_list("channel_id", flat=True))
    assert remaining == [b.pk]


def test_patch_without_target_specs_leaves_existing_targets_untouched(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    channel = _channel(workspace, "A")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    target = PostTarget.objects.create(post=post, channel=channel)

    response = _client_for(user).patch(f"/posts/{post.pk}/", {"base_text": "updated"}, format="json")

    assert response.status_code == 200, response.data
    assert response.data["base_text"] == "updated"
    assert list(post.targets.values_list("pk", flat=True)) == [target.pk]


def test_patch_can_add_a_second_target(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    a, b = _channel(workspace, "A"), _channel(workspace, "B")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    PostTarget.objects.create(post=post, channel=a)

    response = _client_for(user).patch(
        f"/posts/{post.pk}/", {"target_specs": [{"channel": a.pk}, {"channel": b.pk}]}, format="json"
    )

    assert response.status_code == 200, response.data
    assert set(post.targets.values_list("channel_id", flat=True)) == {a.pk, b.pk}


def test_patch_target_specs_rejected_once_scheduled(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    a, b = _channel(workspace, "A"), _channel(workspace, "B")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi", status=Post.STATUS_SCHEDULED)
    target = PostTarget.objects.create(post=post, channel=a, status=PostTarget.STATUS_SCHEDULED)

    response = _client_for(user).patch(
        f"/posts/{post.pk}/", {"target_specs": [{"channel": b.pk}]}, format="json"
    )

    assert response.status_code == 400
    assert "target_specs" in response.data
    assert list(post.targets.values_list("pk", flat=True)) == [target.pk]


def test_patch_target_specs_allowed_on_a_failed_post(db, django_user_model):
    """Mirrors _check_can_schedule's allowed set (draft/failed) — a failed
    post is the other status a user is expected to fix up and retry from."""
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    a, b = _channel(workspace, "A"), _channel(workspace, "B")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi", status=Post.STATUS_FAILED)
    PostTarget.objects.create(post=post, channel=a, status=PostTarget.STATUS_FAILED)

    response = _client_for(user).patch(
        f"/posts/{post.pk}/", {"target_specs": [{"channel": b.pk}]}, format="json"
    )

    assert response.status_code == 200, response.data
    assert list(post.targets.values_list("channel_id", flat=True)) == [b.pk]
