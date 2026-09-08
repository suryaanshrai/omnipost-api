import pytest
import responses
from ai import gateway
from ai.errors import AIConfigurationError, AIProviderError
from omnipost_api.models import ProviderKey, UsageCounter


def _add_provider_key(workspace, provider, api_key="sk-byok-secret"):
    key = ProviderKey.objects.create(workspace=workspace, provider=provider, encrypted_key={})
    from omnipost_api import crypto

    key.encrypted_key = crypto.encrypt(api_key, aad=key._aad())
    key.save(update_fields=["encrypted_key"])
    return key


def test_resolve_text_provider_prefers_byok(workspace, settings):
    settings.AI_MANAGED_TEXT_API_KEY = "managed-key"
    _add_provider_key(workspace, ProviderKey.PROVIDER_ANTHROPIC, api_key="my-own-key")

    resolved = gateway.resolve_text_provider(workspace, None)
    assert resolved.is_byok is True
    assert resolved.api_key == "my-own-key"


def test_resolve_text_provider_falls_back_to_managed(workspace, settings):
    settings.AI_MANAGED_TEXT_API_KEY = "managed-key"
    resolved = gateway.resolve_text_provider(workspace, None)
    assert resolved.is_byok is False
    assert resolved.api_key == "managed-key"


def test_resolve_text_provider_raises_without_any_key(workspace, settings):
    settings.AI_MANAGED_TEXT_API_KEY = ""
    with pytest.raises(AIConfigurationError):
        gateway.resolve_text_provider(workspace, "openai")


@responses.activate
def test_run_text_managed_success_commits_quota(workspace, settings):
    settings.AI_MANAGED_TEXT_API_KEY = "managed-key"
    settings.AI_FREE_TEXT_LIMIT = 3
    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": "hi there"}]},
        status=200,
    )
    result = gateway.run_text(workspace, prompt="hello")
    assert result == "hi there"

    counter = UsageCounter.objects.get(workspace=workspace, feature=UsageCounter.FEATURE_AI_TEXT)
    assert counter.committed == 1
    assert counter.reserved == 0


@responses.activate
def test_run_text_managed_failure_releases_quota(workspace, settings):
    settings.AI_MANAGED_TEXT_API_KEY = "managed-key"
    settings.AI_FREE_TEXT_LIMIT = 3
    responses.add(responses.POST, "https://api.anthropic.com/v1/messages", json={"error": "boom"}, status=500)

    with pytest.raises(AIProviderError):
        gateway.run_text(workspace, prompt="hello")

    counter = UsageCounter.objects.get(workspace=workspace, feature=UsageCounter.FEATURE_AI_TEXT)
    assert counter.committed == 0
    assert counter.reserved == 0


@responses.activate
def test_run_text_byok_does_not_touch_quota(workspace, settings):
    settings.AI_MANAGED_TEXT_API_KEY = "managed-key"
    _add_provider_key(workspace, ProviderKey.PROVIDER_ANTHROPIC)
    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": "hi"}]},
        status=200,
    )
    gateway.run_text(workspace, prompt="hello")
    assert not UsageCounter.objects.filter(workspace=workspace, feature=UsageCounter.FEATURE_AI_TEXT).exists()


@responses.activate
def test_run_image_managed_success_commits_quota(workspace, settings):
    import base64

    settings.AI_MANAGED_IMAGE_API_KEY = "managed-image-key"
    settings.AI_FREE_IMAGE_LIMIT = 1
    responses.add(
        responses.POST,
        "https://api.openai.com/v1/images/generations",
        json={"data": [{"b64_json": base64.b64encode(b"pngdata").decode()}]},
        status=200,
    )
    result = gateway.run_image(workspace, prompt="a cat")
    assert result == b"pngdata"
    counter = UsageCounter.objects.get(workspace=workspace, feature=UsageCounter.FEATURE_AI_IMAGE)
    assert counter.committed == 1
