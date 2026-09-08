import base64

import pytest
import responses
from ai import providers
from ai.errors import AIProviderError
from ai.providers import ImageInput


@responses.activate
def test_generate_text_anthropic_parses_content_blocks():
    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": "hello from claude"}]},
        status=200,
    )
    result = providers.generate_text("anthropic", "sk-ant-x", "claude-sonnet-5", prompt="hi")
    assert result == "hello from claude"

    sent = responses.calls[0].request
    assert sent.headers["x-api-key"] == "sk-ant-x"


@responses.activate
def test_generate_text_anthropic_sends_cache_control_on_system():
    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": "ok"}]},
        status=200,
    )
    providers.generate_text("anthropic", "sk-ant-x", "claude-sonnet-5", prompt="hi", system="be nice")
    import json as _json

    body = _json.loads(responses.calls[0].request.body)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


@responses.activate
def test_generate_text_anthropic_raises_on_error_status():
    responses.add(responses.POST, "https://api.anthropic.com/v1/messages", json={"error": "nope"}, status=401)
    with pytest.raises(AIProviderError):
        providers.generate_text("anthropic", "bad-key", "claude-sonnet-5", prompt="hi")


@responses.activate
def test_generate_text_openai_parses_choices():
    responses.add(
        responses.POST,
        "https://api.openai.com/v1/chat/completions",
        json={"choices": [{"message": {"content": "hello from gpt"}}]},
        status=200,
    )
    result = providers.generate_text("openai", "sk-x", "gpt-4o-mini", prompt="hi")
    assert result == "hello from gpt"

    sent = responses.calls[0].request
    assert sent.headers["Authorization"] == "Bearer sk-x"


@responses.activate
def test_generate_text_with_image_sends_vision_content():
    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": "a red square"}]},
        status=200,
    )
    image = ImageInput(data_b64=base64.b64encode(b"fakebytes").decode(), media_type="image/png")
    result = providers.generate_text("anthropic", "sk-ant-x", "claude-sonnet-5", prompt="describe", image=image)
    assert result == "a red square"

    import json as _json

    body = _json.loads(responses.calls[0].request.body)
    blocks = body["messages"][0]["content"]
    assert any(b["type"] == "image" for b in blocks)


def test_generate_text_image_rejected_for_non_vision_provider():
    image = ImageInput(data_b64="abc", media_type="image/png")
    with pytest.raises(AIProviderError):
        providers.generate_text("openrouter", "key", "model", prompt="describe", image=image)


@responses.activate
def test_generate_image_openai_decodes_b64():
    raw = b"\x89PNG fake bytes"
    responses.add(
        responses.POST,
        "https://api.openai.com/v1/images/generations",
        json={"data": [{"b64_json": base64.b64encode(raw).decode()}]},
        status=200,
    )
    result = providers.generate_image("openai", "sk-x", "gpt-image-1", prompt="a cat", aspect="square_1x1")
    assert result == raw


def test_generate_image_rejects_non_openai_provider():
    with pytest.raises(AIProviderError):
        providers.generate_image("anthropic", "key", "model", prompt="a cat", aspect=None)


@responses.activate
def test_ping_uses_generate_text():
    responses.add(
        responses.POST,
        "https://api.anthropic.com/v1/messages",
        json={"content": [{"type": "text", "text": "ok"}]},
        status=200,
    )
    providers.ping("anthropic", "sk-ant-x", "claude-sonnet-5")
