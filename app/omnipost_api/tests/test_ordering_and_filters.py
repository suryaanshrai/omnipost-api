"""Post/Channel/PostTarget/PublishAttempt/MediaAsset previously declared no
Meta.ordering, so LimitOffsetPagination over them had no deterministic
tie-break — offset pages could return duplicates and gaps as rows changed
between requests. These tests pin the fix: newest-first ordering by default,
plus the PostViewSet `status`/`workspace` filters that stop `/posts/?status=
draft` from silently returning every post regardless of status."""

from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from omnipost_api.models import Channel, MediaAsset, Post, PostTarget, PublishAttempt


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _channel(workspace, name="Mastodon"):
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name=name)
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    return channel


def _backdate(model, pk, minutes_ago):
    # auto_now_add fields can't be set on create(); back-date via .update()
    # (which bypasses save()) so three objects created back-to-back in the
    # same test don't tie at the timestamp resolution available here.
    model.objects.filter(pk=pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))


def test_post_default_ordering_is_newest_first(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    oldest = Post.objects.create(workspace=workspace, kind="text", base_text="oldest")
    middle = Post.objects.create(workspace=workspace, kind="text", base_text="middle")
    newest = Post.objects.create(workspace=workspace, kind="text", base_text="newest")
    _backdate(Post, oldest.pk, 2)
    _backdate(Post, middle.pk, 1)

    assert list(Post.objects.filter(workspace=workspace).values_list("pk", flat=True)) == [
        newest.pk,
        middle.pk,
        oldest.pk,
    ]

    response = _client_for(user).get("/posts/")
    assert response.status_code == 200
    assert [row["id"] for row in response.data["results"]] == [newest.pk, middle.pk, oldest.pk]


def test_channel_post_target_publish_attempt_media_asset_are_newest_first(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace

    c1, c2 = _channel(workspace, "First"), _channel(workspace, "Second")
    _backdate(Channel, c1.pk, 1)
    assert list(Channel.objects.filter(workspace=workspace).values_list("pk", flat=True)) == [c2.pk, c1.pk]

    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    t1 = PostTarget.objects.create(post=post, channel=c1)
    t2 = PostTarget.objects.create(post=post, channel=c1)
    _backdate(PostTarget, t1.pk, 1)
    assert list(PostTarget.objects.filter(post=post).values_list("pk", flat=True)) == [t2.pk, t1.pk]

    a1 = PublishAttempt.objects.create(post_target=t1, run_at=timezone.now())
    a2 = PublishAttempt.objects.create(post_target=t1, epoch=2, run_at=timezone.now())
    _backdate(PublishAttempt, a1.pk, 1)
    assert list(PublishAttempt.objects.filter(post_target=t1).values_list("pk", flat=True)) == [a2.pk, a1.pk]

    m1 = MediaAsset.objects.create(workspace=workspace, kind=MediaAsset.KIND_IMAGE, file="a.jpg")
    m2 = MediaAsset.objects.create(workspace=workspace, kind=MediaAsset.KIND_IMAGE, file="b.jpg")
    _backdate(MediaAsset, m1.pk, 1)
    assert list(MediaAsset.objects.filter(workspace=workspace).values_list("pk", flat=True)) == [m2.pk, m1.pk]


def test_posts_status_filter(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    draft = Post.objects.create(workspace=workspace, kind="text", base_text="draft one")
    Post.objects.create(workspace=workspace, kind="text", base_text="scheduled one", status=Post.STATUS_SCHEDULED)

    response = _client_for(user).get("/posts/", {"status": "draft"})

    assert response.status_code == 200
    ids = [row["id"] for row in response.data["results"]]
    assert ids == [draft.pk]


def test_posts_status_filter_accepts_a_comma_separated_list(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    draft = Post.objects.create(workspace=workspace, kind="text", base_text="draft one")
    failed = Post.objects.create(workspace=workspace, kind="text", base_text="failed one", status=Post.STATUS_FAILED)
    Post.objects.create(workspace=workspace, kind="text", base_text="published one", status=Post.STATUS_PUBLISHED)

    response = _client_for(user).get("/posts/", {"status": "draft,failed"})

    assert response.status_code == 200
    ids = {row["id"] for row in response.data["results"]}
    assert ids == {draft.pk, failed.pk}


def test_posts_unfiltered_still_returns_every_status(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    Post.objects.create(workspace=workspace, kind="text", base_text="draft one")
    Post.objects.create(workspace=workspace, kind="text", base_text="scheduled one", status=Post.STATUS_SCHEDULED)

    response = _client_for(user).get("/posts/")

    assert response.status_code == 200
    assert response.data["count"] == 2


def test_posts_workspace_filter_is_still_scoped_to_the_user(db, django_user_model):
    """The `workspace` query param narrows within the user's own workspaces —
    it must never let a user pass another user's workspace id and see their
    posts (WorkspaceScopedViewSet.get_queryset() already scopes the base
    queryset; the added filter runs on top of that, not instead of it)."""
    owner = django_user_model.objects.create_user(username="owner", password="x")
    other = django_user_model.objects.create_user(username="other", password="x")
    owner_workspace = owner.memberships.get().workspace
    Post.objects.create(workspace=owner_workspace, kind="text", base_text="not yours")

    response = _client_for(other).get("/posts/", {"workspace": owner_workspace.pk})

    assert response.status_code == 200
    assert response.data["count"] == 0
