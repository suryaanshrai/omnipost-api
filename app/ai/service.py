"""High-level AI features, each a thin orchestration over gateway.run_text /
run_image: build a prompt, call the gateway, parse/validate the result,
persist anything that needs it. Views (omnipost_api/views_ai.py) call these
directly rather than gateway.py, so no caller can accidentally skip the
preflight-validation or persistence steps a feature requires.
"""

from __future__ import annotations

import base64
import html
import re

import requests as http
from connectors.base import ChannelCredentials, PublishContext
from connectors.registry import get as get_connector
from django.core.files.base import ContentFile
from jobs.media import RENDITION_PROFILES
from omnipost_api.models import Channel, MediaAsset, VoiceProfile, Workspace

from . import gateway, prompts
from .errors import AIProviderError
from .providers import ImageInput

_URL_FETCH_TIMEOUT_S = 20
_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\n{3,}")


def _extract_text_from_html(markup: str) -> str:
    """Deliberately not a full readability parser — strips script/style
    blocks and remaining tags, then collapses whitespace. Good enough to
    hand a blog post's text to an LLM; not meant to preserve layout."""
    text = _TAG_RE.sub(" ", markup)
    text = _ANY_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WHITESPACE_RE.sub("\n\n", text)
    return text.strip()


def fetch_source_text(url: str) -> str:
    response = http.get(url, timeout=_URL_FETCH_TIMEOUT_S, headers={"User-Agent": "OmniPost/1.0"})
    response.raise_for_status()
    return _extract_text_from_html(response.text)


def _validate_text_for_channel(channel: Channel, text: str) -> list[dict]:
    connector = get_connector(channel.connector_slug)
    ctx = PublishContext(
        text=text, media=[], post_kind="text", credentials=ChannelCredentials(values={}), idempotency_key="ai-preview"
    )
    findings = connector.validate(ctx)
    return [{"code": f.code, "message": f.message, "blocking": f.blocking, "field": f.field} for f in findings]


def generate_variants(
    workspace: Workspace,
    *,
    brief: str,
    channels: list[Channel],
    voice_profile: VoiceProfile | None,
    provider: str | None,
) -> list[dict]:
    channel_specs = [
        {
            "id": channel.pk,
            "display_name": channel.display_name,
            "capabilities": get_connector(channel.connector_slug).capabilities,
        }
        for channel in channels
    ]
    system, user_prompt = prompts.build_variants_prompt(brief, channel_specs, voice_profile)
    raw = gateway.run_text(workspace, prompt=user_prompt, system=system, provider=provider)
    parsed = prompts.extract_json(raw)

    results = []
    for channel in channels:
        text = str(parsed.get(str(channel.pk), "")).strip()
        caps = get_connector(channel.connector_slug).capabilities
        if caps.max_text_length and len(text) > caps.max_text_length:
            # The model is asked to respect this but isn't guaranteed to —
            # this is the backstop that makes "every generated variant
            # passes preflight" an actual guarantee, not just a request.
            text = text[: caps.max_text_length - 1].rstrip() + "…"
        results.append({"channel": channel.pk, "text": text, "findings": _validate_text_for_channel(channel, text)})
    return results


def repurpose_content(
    workspace: Workspace,
    *,
    source_text: str | None,
    source_url: str | None,
    target_formats: list[str],
    voice_profile: VoiceProfile | None,
    provider: str | None,
) -> dict[str, str]:
    text = source_text or (fetch_source_text(source_url) if source_url else "")
    if not text.strip():
        raise AIProviderError("No source content to repurpose — provide source_text or a reachable source_url.")

    system, user_prompt = prompts.build_repurpose_prompt(text, target_formats, voice_profile)
    raw = gateway.run_text(workspace, prompt=user_prompt, system=system, provider=provider)
    parsed = prompts.extract_json(raw)
    return {fmt: str(parsed.get(fmt, "")).strip() for fmt in target_formats}


def generate_alt_text(workspace: Workspace, asset: MediaAsset, *, provider: str | None) -> str:
    if asset.kind != MediaAsset.KIND_IMAGE:
        raise AIProviderError("Alt-text generation only supports image media right now.")
    if not asset.mime_type:
        raise AIProviderError("This media hasn't finished processing yet — try again shortly.")

    with asset.file.open("rb") as fh:
        data_b64 = base64.b64encode(fh.read()).decode()

    system, user_prompt = prompts.build_alt_text_prompt()
    image = ImageInput(data_b64=data_b64, media_type=asset.mime_type)
    text = gateway.run_text(workspace, prompt=user_prompt, system=system, provider=provider, image=image).strip()

    asset.alt_text = text
    asset.save(update_fields=["alt_text"])
    return text


def generate_image_asset(
    workspace: Workspace, *, prompt: str, aspect: str | None, uploaded_by, provider: str | None
) -> MediaAsset:
    if aspect and aspect not in RENDITION_PROFILES:
        raise AIProviderError(f"Unknown aspect profile '{aspect}'. Choose one of: {', '.join(RENDITION_PROFILES)}.")

    image_bytes = gateway.run_image(workspace, prompt=prompt, aspect=aspect, provider=provider)

    asset = MediaAsset(workspace=workspace, uploaded_by=uploaded_by, kind=MediaAsset.KIND_IMAGE)
    asset.file.save("ai-generated.png", ContentFile(image_bytes), save=False)
    asset.save()

    from jobs.queue import enqueue_probe

    enqueue_probe(asset.pk)
    return asset
