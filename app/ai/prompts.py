"""Prompt construction. Kept separate from service.py so the actual wording
can be iterated on without touching orchestration logic (quota, validation,
persistence).

Every prompt that expects structured output asks for raw JSON and nothing
else — _extract_json then strips the markdown code-fence models add anyway
despite being told not to.
"""

from __future__ import annotations

import json
import re

from connectors.capabilities import Capabilities

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(text: str) -> dict:
    cleaned = _JSON_FENCE_RE.sub("", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Model response was not valid JSON: {exc}") from exc


def voice_style_card(voice_profile) -> str:
    if voice_profile is None:
        return ""
    parts = []
    if voice_profile.style_summary:
        parts.append(f"Brand voice: {voice_profile.style_summary}")
    examples = list(voice_profile.example_posts or [])[:5]
    if examples:
        formatted = "\n".join(f"- {example}" for example in examples)
        parts.append(f"Examples of this brand's past posts (match this tone and style):\n{formatted}")
    return "\n\n".join(parts)


_VARIANTS_SYSTEM = (
    "You are a social media copywriter producing platform-native post variants "
    "from one idea. Respond with ONLY a single JSON object mapping each given "
    "channel id (as a string) to the generated post text for that channel — "
    "no markdown fences, no commentary, no keys other than the channel ids given."
)


def build_variants_prompt(brief: str, channel_specs: list[dict], voice_profile=None) -> tuple[str, str]:
    """channel_specs: [{"id": int, "display_name": str, "capabilities": Capabilities}]
    Returns (system, user_prompt)."""
    lines = [f"Post idea: {brief}", ""]
    style = voice_style_card(voice_profile)
    if style:
        lines.append(style)
        lines.append("")
    lines.append("Generate one variant per channel below, respecting its constraints exactly:")
    for spec in channel_specs:
        caps: Capabilities = spec["capabilities"]
        constraints = []
        if caps.max_text_length:
            constraints.append(f"max {caps.max_text_length} characters")
        if caps.hashtag_syntax:
            constraints.append(f"hashtags as {caps.hashtag_syntax}")
        if caps.mention_syntax:
            constraints.append(f"mentions as {caps.mention_syntax}")
        if not caps.supports_link:
            constraints.append("do not include a raw URL in the text")
        constraint_text = "; ".join(constraints) or "no special constraints"
        lines.append(f'- channel id "{spec["id"]}" ({spec["display_name"]}): {constraint_text}')
    return _VARIANTS_SYSTEM, "\n".join(lines)


_REPURPOSE_SYSTEM = (
    "You repurpose one piece of source content into other social formats. "
    "Respond with ONLY a single JSON object mapping each requested format name "
    "(exactly as given) to the generated text for that format — no markdown "
    "fences, no commentary, no keys other than the format names given."
)


def build_repurpose_prompt(source_text: str, target_formats: list[str], voice_profile=None) -> tuple[str, str]:
    lines = [f"Source content:\n{source_text}", ""]
    style = voice_style_card(voice_profile)
    if style:
        lines.append(style)
        lines.append("")
    lines.append("Produce a version of this content for each of these formats: " + ", ".join(target_formats))
    return _REPURPOSE_SYSTEM, "\n".join(lines)


_ALT_TEXT_SYSTEM = (
    "You write concise, accurate alt text for images, for accessibility. "
    "Describe what's visually in the image in one or two plain sentences. "
    "Do not start with 'image of' or 'picture of'. Respond with ONLY the alt "
    "text itself, no quotes, no commentary."
)


def build_alt_text_prompt() -> tuple[str, str]:
    return _ALT_TEXT_SYSTEM, "Write alt text for this image."
