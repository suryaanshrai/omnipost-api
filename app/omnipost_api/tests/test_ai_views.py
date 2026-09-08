from __future__ import annotations

import base64
import json

import responses
from rest_framework.test import APIClient

from omnipost_api.models import Channel, MediaAsset


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _mastodon_channel_for(user) -> Channel:
    workspace = user.memberships.get().workspace
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="My Mastodon")
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    return channel


def _mock_anthropic_text(payload: dict, status: int = 200) -> None:
    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": json.dumps(payload)}]} if status == 200 else {"error": "boom"},
        status=status,
    )


@responses.activate
def test_generate_variants_happy_path(db, django_user_model):
    user = django_user_model.objects.create_user(username="finn", password="x")
    channel = _mastodon_channel_for(user)
    _mock_anthropic_text({str(channel.pk): "check out our launch!"})

    response = _client_for(user).post(
        "/ai/variants/",
        {"workspace": channel.workspace_id, "brief": "we launched", "channels": [channel.pk]},
        format="json",
    )
    assert response.status_code == 200
    variant = response.data["variants"][0]
    assert variant["channel"] == channel.pk
    assert variant["text"] == "check out our launch!"
    assert variant["findings"] == []


@responses.activate
def test_generate_variants_rejects_channel_outside_workspace(db, django_user_model):
    owner = django_user_model.objects.create_user(username="gina", password="x")
    channel = _mastodon_channel_for(owner)

    other = django_user_model.objects.create_user(username="hank", password="x")
    other_workspace = other.memberships.get().workspace

    response = _client_for(other).post(
        "/ai/variants/", {"workspace": other_workspace.pk, "brief": "hi", "channels": [channel.pk]}, format="json"
    )
    assert response.status_code == 400


def test_generate_variants_requires_authentication(db, django_user_model):
    user = django_user_model.objects.create_user(username="ivy", password="x")
    channel = _mastodon_channel_for(user)
    response = APIClient().post(
        "/ai/variants/", {"workspace": channel.workspace_id, "brief": "hi", "channels": [channel.pk]}, format="json"
    )
    assert response.status_code == 401


@responses.activate
def test_free_tier_allows_exactly_three_text_generations(db, django_user_model, settings):
    user = django_user_model.objects.create_user(username="jack", password="x")
    channel = _mastodon_channel_for(user)
    client = _client_for(user)

    for _ in range(settings.AI_FREE_TEXT_LIMIT):
        _mock_anthropic_text({str(channel.pk): "a variant"})
        response = client.post(
            "/ai/variants/", {"workspace": channel.workspace_id, "brief": "hi", "channels": [channel.pk]}, format="json"
        )
        assert response.status_code == 200

    # No 4th mock registered — if the view tried to call out to Anthropic
    # again, `responses` would raise ConnectionError before this assertion.
    response = client.post(
        "/ai/variants/", {"workspace": channel.workspace_id, "brief": "hi", "channels": [channel.pk]}, format="json"
    )
    assert response.status_code == 400
    assert "quota" in json.dumps(response.data).lower() or "limit" in json.dumps(response.data).lower()


@responses.activate
def test_failed_provider_call_does_not_consume_quota(db, django_user_model):
    user = django_user_model.objects.create_user(username="kim", password="x")
    channel = _mastodon_channel_for(user)
    client = _client_for(user)

    _mock_anthropic_text({}, status=500)
    response = client.post(
        "/ai/variants/", {"workspace": channel.workspace_id, "brief": "hi", "channels": [channel.pk]}, format="json"
    )
    assert response.status_code == 502

    usage = client.get(f"/ai/usage/?workspace={channel.workspace_id}")
    assert usage.data["ai_text_generation"]["committed"] == 0
    assert usage.data["ai_text_generation"]["reserved"] == 0

    # And the freed slot is still usable.
    _mock_anthropic_text({str(channel.pk): "now it works"})
    response = client.post(
        "/ai/variants/", {"workspace": channel.workspace_id, "brief": "hi", "channels": [channel.pk]}, format="json"
    )
    assert response.status_code == 200


@responses.activate
def test_byok_key_never_appears_in_response_or_logs(db, django_user_model, caplog):
    user = django_user_model.objects.create_user(username="liam", password="x")
    channel = _mastodon_channel_for(user)
    client = _client_for(user)
    secret_key = "sk-ant-super-secret-value-12345"

    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": "ok"}]},
        status=200,
    )
    key_response = client.post(
        "/provider-keys/",
        {"workspace": channel.workspace_id, "provider": "anthropic", "api_key": secret_key},
        format="json",
    )
    assert key_response.status_code == 201
    assert "api_key" not in key_response.data
    assert secret_key not in json.dumps(key_response.data)

    with caplog.at_level("DEBUG"):
        _mock_anthropic_text({str(channel.pk): "byok variant"})
        response = client.post(
            "/ai/variants/",
            {"workspace": channel.workspace_id, "brief": "hi", "channels": [channel.pk], "provider": "anthropic"},
            format="json",
        )
    assert response.status_code == 200
    assert secret_key not in json.dumps(response.data)
    assert not any(secret_key in record.getMessage() for record in caplog.records)


@responses.activate
def test_provider_key_create_rejects_invalid_key(db, django_user_model):
    user = django_user_model.objects.create_user(username="mona", password="x")
    workspace = user.memberships.get().workspace
    responses.add(responses.POST, "https://api.anthropic.com/v1/messages", json={"error": "unauthorized"}, status=401)

    response = _client_for(user).post(
        "/provider-keys/", {"workspace": workspace.pk, "provider": "anthropic", "api_key": "bad-key"}, format="json"
    )
    assert response.status_code == 400


@responses.activate
def test_repurpose_endpoint(db, django_user_model):
    user = django_user_model.objects.create_user(username="nina", password="x")
    workspace = user.memberships.get().workspace
    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": json.dumps({"thread": "a thread version"})}]},
        status=200,
    )
    response = _client_for(user).post(
        "/ai/repurpose/",
        {"workspace": workspace.pk, "source_text": "a long blog post", "target_formats": ["thread"]},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["results"]["thread"] == "a thread version"


def test_repurpose_requires_a_source(db, django_user_model):
    user = django_user_model.objects.create_user(username="omar", password="x")
    workspace = user.memberships.get().workspace
    response = _client_for(user).post(
        "/ai/repurpose/", {"workspace": workspace.pk, "target_formats": ["thread"]}, format="json"
    )
    assert response.status_code == 400


@responses.activate
def test_alt_text_endpoint_updates_asset(db, django_user_model):
    from django.core.files.base import ContentFile

    user = django_user_model.objects.create_user(username="petra", password="x")
    workspace = user.memberships.get().workspace
    asset = MediaAsset(workspace=workspace, kind=MediaAsset.KIND_IMAGE, mime_type="image/png")
    asset.file.save("t.png", ContentFile(b"fakepng"), save=False)
    asset.save()

    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": "a small blue square"}]},
        status=200,
    )
    response = _client_for(user).post("/ai/alt-text/", {"media": asset.pk}, format="json")
    assert response.status_code == 200
    assert response.data["alt_text"] == "a small blue square"
    asset.refresh_from_db()
    assert asset.alt_text == "a small blue square"


@responses.activate
def test_generate_image_free_tier_limit_is_one(db, django_user_model, settings, monkeypatch):
    monkeypatch.setattr("jobs.queue.enqueue_probe", lambda asset_id: None)
    user = django_user_model.objects.create_user(username="quinn", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    for _ in range(settings.AI_FREE_IMAGE_LIMIT):
        responses.add(
            responses.POST,
            "https://api.openai.com/v1/images/generations",
            json={"data": [{"b64_json": base64.b64encode(b"pngdata").decode()}]},
            status=200,
        )
        response = client.post("/ai/images/", {"workspace": workspace.pk, "prompt": "a cat"}, format="json")
        assert response.status_code == 201

    response = client.post("/ai/images/", {"workspace": workspace.pk, "prompt": "a cat"}, format="json")
    assert response.status_code == 400
