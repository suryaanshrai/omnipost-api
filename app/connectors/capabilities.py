"""The capabilities manifest: one declarative description of what a platform
can accept, consumed by three different things — composer validation,
the publish-time guard, and the constraints handed to the AI rewriter (Phase 4).
Previously this knowledge didn't exist anywhere: text-length limits, media
rules, and rate limits were implicit in whatever the hand-typed request JSON
happened to do, discovered only when a publish failed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MediaRule:
    kinds: tuple[str, ...] = ()  # "image", "video", "gif"
    max_count: int = 0
    max_size_mb: float | None = None
    max_duration_s: float | None = None
    min_duration_s: float | None = None
    allowed_aspect_ratios: tuple[str, ...] = ()  # e.g. "1:1", "4:5", "9:16"
    allowed_mime_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class RateLimit:
    max_requests: int
    per_seconds: int
    scope: str = "channel"  # "channel" or "app"


@dataclass(frozen=True)
class Capabilities:
    slug: str
    display_name: str
    max_text_length: int | None = None
    supports_link: bool = True
    supports_alt_text: bool = False
    supports_threads: bool = False
    supports_scheduling_native: bool = False
    hashtag_syntax: str | None = "#tag"
    mention_syntax: str | None = "@user"
    media: tuple[MediaRule, ...] = ()
    rate_limits: tuple[RateLimit, ...] = field(default_factory=tuple)
    post_kinds: tuple[str, ...] = ("text",)  # text, image, video, story, short_video

    def media_rule_for(self, kind: str) -> MediaRule | None:
        for rule in self.media:
            if kind in rule.kinds:
                return rule
        return None
