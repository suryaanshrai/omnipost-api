"""The single entry point every AI feature (service.py) calls through:
resolves which provider/key/model a workspace should use for a given
feature, meters managed-provider usage against quota, and never lets a
failed provider call burn that quota. BYOK calls skip quota entirely — the
workspace is spending its own key's balance, not OmniPost's.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from omnipost_api.models import ProviderKey, UsageCounter, Workspace

from . import providers, quota
from .errors import AIConfigurationError
from .providers import ImageInput


@dataclass(frozen=True)
class ResolvedProvider:
    provider: str
    api_key: str
    model: str
    is_byok: bool


def _byok_key(workspace: Workspace, provider: str) -> ProviderKey | None:
    return ProviderKey.objects.filter(workspace=workspace, provider=provider, is_active=True).first()


def resolve_text_provider(workspace: Workspace, provider: str | None) -> ResolvedProvider:
    provider = provider or settings.AI_DEFAULT_TEXT_PROVIDER
    key = _byok_key(workspace, provider)
    if key is not None:
        return ResolvedProvider(
            provider=provider, api_key=key.get_key(), model=settings.AI_TEXT_MODELS[provider], is_byok=True
        )
    if provider == settings.AI_DEFAULT_TEXT_PROVIDER and settings.AI_MANAGED_TEXT_API_KEY:
        return ResolvedProvider(
            provider=provider, api_key=settings.AI_MANAGED_TEXT_API_KEY, model=settings.AI_DEFAULT_TEXT_MODEL,
            is_byok=False,
        )
    raise AIConfigurationError(
        f"No usable '{provider}' credentials — add a BYOK key for this workspace, "
        "or this deployment hasn't configured a managed key for it."
    )


def resolve_image_provider(workspace: Workspace, provider: str | None) -> ResolvedProvider:
    provider = provider or settings.AI_DEFAULT_IMAGE_PROVIDER
    key = _byok_key(workspace, provider)
    if key is not None:
        return ResolvedProvider(
            provider=provider, api_key=key.get_key(), model=settings.AI_DEFAULT_IMAGE_MODEL, is_byok=True
        )
    if provider == settings.AI_DEFAULT_IMAGE_PROVIDER and settings.AI_MANAGED_IMAGE_API_KEY:
        return ResolvedProvider(
            provider=provider, api_key=settings.AI_MANAGED_IMAGE_API_KEY, model=settings.AI_DEFAULT_IMAGE_MODEL,
            is_byok=False,
        )
    raise AIConfigurationError(
        f"No usable '{provider}' credentials for image generation — add a BYOK key for this "
        "workspace, or this deployment hasn't configured a managed image provider."
    )


def run_text(
    workspace: Workspace,
    *,
    prompt: str,
    system: str | None = None,
    provider: str | None = None,
    image: ImageInput | None = None,
) -> str:
    resolved = resolve_text_provider(workspace, provider)
    reservation = None
    if not resolved.is_byok:
        reservation = quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)
    try:
        text = providers.generate_text(
            resolved.provider, resolved.api_key, resolved.model, prompt=prompt, system=system, image=image
        )
    except Exception:
        if reservation is not None:
            quota.release(reservation)
        raise
    if reservation is not None:
        quota.commit(reservation)
    return text


def run_image(workspace: Workspace, *, prompt: str, aspect: str | None = None, provider: str | None = None) -> bytes:
    resolved = resolve_image_provider(workspace, provider)
    reservation = None
    if not resolved.is_byok:
        reservation = quota.reserve(workspace, UsageCounter.FEATURE_AI_IMAGE)
    try:
        image_bytes = providers.generate_image(
            resolved.provider, resolved.api_key, resolved.model, prompt=prompt, aspect=aspect
        )
    except Exception:
        if reservation is not None:
            quota.release(reservation)
        raise
    if reservation is not None:
        quota.commit(reservation)
    return image_bytes
