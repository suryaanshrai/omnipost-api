"""Thin REST clients for each supported provider. Deliberately raw
`requests` calls rather than each vendor's SDK — every provider's chat
endpoint is a simple JSON-in/JSON-out POST, and a single HTTP layer means
tests can mock all of them uniformly with `responses` instead of learning
four different client libraries' internals (and, for the SDKs that wrap
httpx instead of requests, not being mockable by `responses` at all).

Anthropic's Messages API is used directly for the "anthropic" provider.
OpenAI, OpenRouter, and Gemini all speak an OpenAI-compatible
`/chat/completions` shape (Gemini via its own OpenAI-compatibility layer),
so one function serves all three — see _OPENAI_COMPAT_BASE_URLS.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

import requests

from .errors import AIProviderError

_TIMEOUT_S = 60
_ANTHROPIC_VERSION = "2023-06-01"
_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"

_OPENAI_COMPAT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
}

# Providers whose vision input this module knows how to build. Anthropic and
# OpenAI-shaped chat APIs both accept inline base64 image content; Gemini's
# OpenAI-compat layer does too, so it rides along for free. OpenRouter routes
# to many underlying models with inconsistent vision support, so it's left
# out until there's a concrete need to pick a specific vision-capable one.
_VISION_CAPABLE_PROVIDERS = {"anthropic", "openai", "gemini"}


@dataclass(frozen=True)
class ImageInput:
    data_b64: str
    media_type: str


def _anthropic_headers(api_key: str) -> dict[str, str]:
    return {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_VERSION,
        "content-type": "application/json",
    }


def _generate_text_anthropic(
    api_key: str, model: str, system: str | None, prompt: str, image: ImageInput | None
) -> str:
    content: list[dict] = []
    if image is not None:
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": image.media_type, "data": image.data_b64}}
        )
    content.append({"type": "text", "text": prompt})

    body: dict = {
        "model": model,
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": content}],
    }
    if system:
        # A cache_control breakpoint on the system prompt lets the (fairly
        # large, mostly-static) brand-voice/capability instructions be
        # reused across a workspace's generation calls instead of being
        # re-processed from scratch every time.
        body["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    response = requests.post(
        f"{_ANTHROPIC_BASE_URL}/messages", headers=_anthropic_headers(api_key), json=body, timeout=_TIMEOUT_S
    )
    if not response.ok:
        raise AIProviderError(f"Anthropic request failed ({response.status_code}).")
    data = response.json()
    blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
    if not blocks:
        raise AIProviderError("Anthropic returned no text content.")
    return "".join(blocks)


def _generate_text_openai_compat(
    provider: str, api_key: str, model: str, system: str | None, prompt: str, image: ImageInput | None
) -> str:
    base_url = _OPENAI_COMPAT_BASE_URLS[provider]
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})

    if image is not None:
        user_content: list[dict] | str = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{image.media_type};base64,{image.data_b64}"},
            },
        ]
    else:
        user_content = prompt
    messages.append({"role": "user", "content": user_content})

    response = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        json={"model": model, "messages": messages},
        timeout=_TIMEOUT_S,
    )
    if not response.ok:
        raise AIProviderError(f"{provider} request failed ({response.status_code}).")
    data = response.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as exc:
        raise AIProviderError(f"{provider} returned an unexpected response shape.") from exc


def generate_text(
    provider: str,
    api_key: str,
    model: str,
    *,
    prompt: str,
    system: str | None = None,
    image: ImageInput | None = None,
) -> str:
    if image is not None and provider not in _VISION_CAPABLE_PROVIDERS:
        raise AIProviderError(f"{provider} does not support image input for this feature.")
    if provider == "anthropic":
        return _generate_text_anthropic(api_key, model, system, prompt, image)
    if provider in _OPENAI_COMPAT_BASE_URLS:
        return _generate_text_openai_compat(provider, api_key, model, system, prompt, image)
    raise AIProviderError(f"Unknown text provider '{provider}'.")


_IMAGE_SIZE_BY_ASPECT = {
    "square_1x1": "1024x1024",
    "portrait_4x5": "1024x1536",
    "vertical_9x16": "1024x1536",
    "landscape_16x9": "1536x1024",
}


def generate_image(provider: str, api_key: str, model: str, *, prompt: str, aspect: str | None) -> bytes:
    if provider != "openai":
        raise AIProviderError(f"{provider} does not support image generation yet.")

    size = _IMAGE_SIZE_BY_ASPECT.get(aspect or "", "1024x1024")
    response = requests.post(
        f"{_OPENAI_COMPAT_BASE_URLS['openai']}/images/generations",
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        json={"model": model, "prompt": prompt, "size": size, "n": 1},
        timeout=120,
    )
    if not response.ok:
        raise AIProviderError(f"openai image generation failed ({response.status_code}).")
    data = response.json()
    try:
        b64 = data["data"][0]["b64_json"]
    except (KeyError, IndexError) as exc:
        raise AIProviderError("openai returned an unexpected image response shape.") from exc
    return base64.b64decode(b64)


def ping(provider: str, api_key: str, model: str) -> None:
    """A minimal, cheap request that fails the same way a real generation
    call would on a bad key — used to validate a BYOK key at save time
    instead of letting the user discover it's wrong on their first real
    generation."""
    generate_text(provider, api_key, model, prompt="Reply with just: ok", system=None)
