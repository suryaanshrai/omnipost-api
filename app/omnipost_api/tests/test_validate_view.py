from rest_framework.test import APIClient

from omnipost_api.models import Channel


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _mastodon_channel_for(user) -> Channel:
    workspace = user.memberships.get().workspace
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="My Mastodon")
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    return channel


def test_validate_clean_text_post_is_ok(db, django_user_model):
    user = django_user_model.objects.create_user(username="finn", password="x")
    channel = _mastodon_channel_for(user)
    client = _client_for(user)

    response = client.post("/validate/", {"channel": channel.pk, "text": "hello world"}, format="json")
    assert response.status_code == 200
    assert response.data["ok"] is True
    assert response.data["findings"] == []


def test_validate_flags_text_too_long_for_mastodon(db, django_user_model):
    user = django_user_model.objects.create_user(username="gina", password="x")
    channel = _mastodon_channel_for(user)
    client = _client_for(user)

    response = client.post("/validate/", {"channel": channel.pk, "text": "x" * 501}, format="json")
    assert response.status_code == 200
    assert response.data["ok"] is False
    assert any(f["code"] == "text_too_long" for f in response.data["findings"])


def test_validate_flags_unsupported_post_kind(db, django_user_model):
    user = django_user_model.objects.create_user(username="hank", password="x")
    channel = _mastodon_channel_for(user)
    client = _client_for(user)

    response = client.post(
        "/validate/", {"channel": channel.pk, "text": "caption", "post_kind": "image"}, format="json"
    )
    assert response.status_code == 200
    assert any(f["code"] == "unsupported_post_kind" for f in response.data["findings"])


def test_validate_rejects_channel_outside_users_workspace(db, django_user_model):
    owner = django_user_model.objects.create_user(username="ivy", password="x")
    channel = _mastodon_channel_for(owner)

    intruder = django_user_model.objects.create_user(username="jack", password="x")
    client = _client_for(intruder)

    response = client.post("/validate/", {"channel": channel.pk, "text": "hi"}, format="json")
    assert response.status_code == 403


def test_validate_requires_authentication(db, django_user_model):
    user = django_user_model.objects.create_user(username="kim", password="x")
    channel = _mastodon_channel_for(user)

    client = APIClient()
    response = client.post("/validate/", {"channel": channel.pk, "text": "hi"}, format="json")
    assert response.status_code == 401
