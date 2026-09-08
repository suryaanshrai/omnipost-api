from rest_framework.test import APIClient

from omnipost_api.models import Channel, Post, PostMetric, PostTarget


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _channel(workspace):
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="Mastodon")
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    return channel


def _published_target(workspace, channel):
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    return PostTarget.objects.create(
        post=post, channel=channel, status=PostTarget.STATUS_PUBLISHED, remote_id="999"
    )


def test_post_target_performance_returns_metric_history(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    channel = _channel(workspace)
    target = _published_target(workspace, channel)
    PostMetric.objects.create(post_target=target, likes=5, comments=1, shares=2)

    response = _client_for(user).get(f"/post-targets/{target.pk}/performance/")

    assert response.status_code == 200
    assert len(response.data) == 1
    assert response.data[0]["likes"] == 5


def test_post_performance_reports_latest_metric_per_target(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    channel = _channel(workspace)
    target = _published_target(workspace, channel)
    PostMetric.objects.create(post_target=target, likes=1)
    PostMetric.objects.create(post_target=target, likes=9)  # the latest one

    response = _client_for(user).get(f"/posts/{target.post_id}/performance/")

    assert response.status_code == 200
    assert len(response.data) == 1
    assert response.data[0]["post_target"] == target.pk
    assert response.data[0]["metric"]["likes"] == 9


def test_post_performance_omits_targets_without_metrics(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    channel = _channel(workspace)
    target = _published_target(workspace, channel)  # no PostMetric created

    response = _client_for(user).get(f"/posts/{target.post_id}/performance/")

    assert response.status_code == 200
    assert response.data == []


def test_channel_best_times_returns_empty_with_no_history(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    channel = _channel(workspace)

    response = _client_for(user).get(f"/channels/{channel.pk}/best-times/")

    assert response.status_code == 200
    assert response.data == []


def test_cannot_see_another_workspaces_post_performance(db, django_user_model):
    owner = django_user_model.objects.create_user(username="owner", password="x")
    other = django_user_model.objects.create_user(username="other", password="x")
    workspace = owner.memberships.get().workspace
    channel = _channel(workspace)
    target = _published_target(workspace, channel)

    response = _client_for(other).get(f"/posts/{target.post_id}/performance/")
    assert response.status_code == 404
