"""Connector.validate() is the shared, capability-driven check every
connector inherits — previously untested, and two of its rules (media size,
aspect ratio) were dead stubs (see git history of base.py) that never fired.
Tested here against a minimal fake connector rather than a real platform,
since no shipped connector yet declares aspect-ratio constraints (that
lands with the gated platforms in Phase 6) — the logic is generic and
should be correct regardless of which connector eventually exercises it."""

from __future__ import annotations

from typing import Any

from connectors.base import (
    ChannelCredentials,
    Connector,
    ContentPart,
    MediaRef,
    PublishContext,
    PublishOutcome,
    PublishState,
)
from connectors.capabilities import Capabilities, MediaRule


class FakeConnector(Connector):
    slug = "fake"
    capabilities = Capabilities(
        slug="fake",
        display_name="Fake",
        max_text_length=10,
        post_kinds=("text", "image", "video"),
        media=(
            MediaRule(
                kinds=("image",),
                max_count=2,
                max_size_mb=1,
                allowed_aspect_ratios=("1:1", "4:5"),
            ),
            MediaRule(
                kinds=("video",),
                max_count=1,
                max_duration_s=60,
                min_duration_s=3,
            ),
        ),
    )

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        raise NotImplementedError


def _ctx(**overrides: Any) -> PublishContext:
    defaults: dict[str, Any] = dict(
        text="short",
        media=[],
        post_kind="text",
        credentials=ChannelCredentials(values={}),
        idempotency_key="k",
    )
    defaults.update(overrides)
    return PublishContext(**defaults)


def test_validate_passes_clean_post():
    assert FakeConnector().validate(_ctx()) == []


def test_text_too_long():
    findings = FakeConnector().validate(_ctx(text="way too long for this platform"))
    assert any(f.code == "text_too_long" for f in findings)


def test_unsupported_post_kind():
    findings = FakeConnector().validate(_ctx(post_kind="story"))
    assert any(f.code == "unsupported_post_kind" for f in findings)


def test_unsupported_media_kind():
    ref = MediaRef(url="u", kind="gif", mime_type="image/gif")
    findings = FakeConnector().validate(_ctx(media=[ref]))
    assert any(f.code == "unsupported_media_kind" for f in findings)


def test_media_too_large_is_blocking():
    ref = MediaRef(url="u", kind="image", mime_type="image/jpeg", size_bytes=2 * 1024 * 1024, width=1000, height=1000)
    findings = FakeConnector().validate(_ctx(post_kind="image", media=[ref]))
    assert any(f.code == "media_too_large" for f in findings)


def test_media_under_size_limit_is_clean():
    ref = MediaRef(url="u", kind="image", mime_type="image/jpeg", size_bytes=500 * 1024, width=1000, height=1000)
    findings = FakeConnector().validate(_ctx(post_kind="image", media=[ref]))
    assert findings == []


def test_unsupported_aspect_ratio_is_blocking():
    # 1080x1350 is 4:5 — allowed. 1080x1920 (9:16) is not in the allowed list.
    ref = MediaRef(url="u", kind="image", mime_type="image/jpeg", width=1080, height=1920)
    findings = FakeConnector().validate(_ctx(post_kind="image", media=[ref]))
    assert any(f.code == "unsupported_aspect_ratio" for f in findings)


def test_allowed_aspect_ratio_is_clean_even_off_by_rounding():
    # 1080x1349 is 4:5.004 — within the 2% tolerance of an exact 4:5 crop.
    ref = MediaRef(url="u", kind="image", mime_type="image/jpeg", width=1080, height=1349)
    findings = FakeConnector().validate(_ctx(post_kind="image", media=[ref]))
    assert findings == []


def test_video_too_long():
    ref = MediaRef(url="u", kind="video", mime_type="video/mp4", duration_s=90)
    findings = FakeConnector().validate(_ctx(post_kind="video", media=[ref]))
    assert any(f.code == "media_too_long" for f in findings)


def test_video_too_short():
    ref = MediaRef(url="u", kind="video", mime_type="video/mp4", duration_s=1)
    findings = FakeConnector().validate(_ctx(post_kind="video", media=[ref]))
    assert any(f.code == "media_too_short" for f in findings)


def test_too_many_media_items():
    refs = [MediaRef(url="u", kind="image", mime_type="image/jpeg") for _ in range(3)]
    findings = FakeConnector().validate(_ctx(post_kind="image", media=refs))
    assert any(f.code == "too_many_media_items" for f in findings)


def test_parts_rejected_when_connector_does_not_support_threads():
    # FakeConnector's Capabilities never sets supports_threads=True.
    findings = FakeConnector().validate(_ctx(parts=[ContentPart(text="a"), ContentPart(text="b")]))
    assert any(f.code == "threads_not_supported" for f in findings)


def test_part_text_too_long_is_blocking():
    findings = FakeConnector().validate(_ctx(parts=[ContentPart(text="way over the ten char limit")]))
    assert any(f.code == "part_text_too_long" for f in findings)
