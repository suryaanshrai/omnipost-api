"""The connector interface every platform integration implements.

Replaces the old system entirely: `Platform.config` (a hand-edited JSON blob),
`replace_keys()` (undelimited string-replace on serialized JSON — see
omnipost_api/models.py's old send_request, now removed), and `run_action()`
(fixed-delay RQ scheduling of each HTTP step with no result checking between
steps). See app/connectors/README.md... actually see the plan doc for the
full rationale; the short version is: a multi-step publish (create container,
poll, publish) is one resumable unit of work now, not N independently
scheduled jobs that fire whether or not the previous one succeeded.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .capabilities import Capabilities
from .errors import Finding


@dataclass(frozen=True)
class MediaRef:
    url: str
    kind: str  # "image" | "video" | "gif"
    mime_type: str
    alt_text: str | None = None
    duration_s: float | None = None
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None


def _matches_any_aspect_ratio(width: int, height: int, allowed: tuple[str, ...], tolerance: float = 0.02) -> bool:
    """True if width:height is within `tolerance` (relative) of any of the
    allowed "W:H" ratio strings — an exact-fraction match would reject real
    photos/video whose pixel dimensions don't reduce to a clean ratio."""
    if height <= 0:
        return False
    actual = width / height
    for spec in allowed:
        w_str, _, h_str = spec.partition(":")
        try:
            target = int(w_str) / int(h_str)
        except (ValueError, ZeroDivisionError):
            continue
        if abs(actual - target) / target <= tolerance:
            return True
    return False


@dataclass(frozen=True)
class ChannelCredentials:
    """Decrypted credential bag for one channel. Never logged, never persisted
    outside the encrypted Credential row it was decrypted from — see
    omnipost_api/crypto.py. Held only for the duration of one connector call."""

    values: dict[str, str]
    external_account_id: str | None = None

    def __getitem__(self, key: str) -> str:
        return self.values[key]

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.values.get(key, default)


@dataclass(frozen=True)
class ContentPart:
    """One post in a thread / one step of a drip. `text`/`media` alone
    (PublishContext.text/media) always describe the first part — a connector
    that doesn't understand `parts` still posts something reasonable. See
    PostTargetPart and Connector.validate()'s supports_threads check."""

    text: str
    media: list[MediaRef] = field(default_factory=list)
    delay_after_s: int = 0


@dataclass
class PublishContext:
    text: str
    media: list[MediaRef]
    post_kind: str  # "text" | "image" | "video" | "story" | "short_video"
    credentials: ChannelCredentials
    idempotency_key: str
    link: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    # Multi-part content (thread/drip). None or empty means "single part" —
    # every existing connector, which only reads .text/.media, is unaffected.
    parts: list[ContentPart] | None = None


@dataclass
class PublishState:
    """Resumable state for a multi-step publish. `step` is a connector-defined
    label (e.g. "container_created", "awaiting_processing"); `data` carries
    whatever the connector needs to resume (container ids, upload session
    urls, etc). Stored as PublishAttempt.state_data — see the job engine."""

    step: str = "start"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishOutcome:
    done: bool
    state: PublishState | None = None
    remote_id: str | None = None
    permalink: str | None = None
    # If not done and no explicit retry time is given, the job engine applies
    # its own backoff. Connectors set this when the platform tells them
    # exactly when to check back (e.g. "container not ready, poll in 3s").
    retry_after_s: float | None = None


@dataclass(frozen=True)
class OAuthAuthorization:
    authorize_url: str
    state: str


class Connector(ABC):
    """One platform integration. Subclass this directly for platforms with
    real state machines (media processing, chunked upload); use
    DeclarativeConnector for simple single-request REST platforms."""

    slug: str
    capabilities: Capabilities
    # True for connectors that must run under the *workspace's own* OAuth
    # app rather than a single OmniPost-managed one (LinkedIn, X) — see
    # omnipost_api.models.AppCredential. The oauth service looks this up and
    # folds {"client_id", "client_secret"} into the `extra` kwargs passed to
    # authorize_url()/exchange_code() below, so the connector just reads them
    # from `extra` like any other caller-supplied value.
    requires_own_app: bool = False
    # Credential keys a user can paste directly (POST /channels/ with
    # `credentials`) for platforms with no OAuth flow, or as an alternative
    # to it. Empty means "connect via OAuth only". Exposed by
    # GET /connectors/ so the Connect modal renders the right fields.
    credential_fields: tuple[str, ...] = ()
    # Inputs authorize_url() needs from the user before the redirect can
    # even be built (e.g. Mastodon's instance domain). Passed as `extra` to
    # POST /oauth/start/.
    oauth_extra_fields: tuple[str, ...] = ()

    @property
    def supports_oauth(self) -> bool:
        return type(self).authorize_url is not Connector.authorize_url

    def validate(self, ctx: PublishContext) -> list[Finding]:
        """Capability-driven validation shared by every connector. Subclasses
        override to add platform-specific checks, calling super().validate()
        first."""
        findings: list[Finding] = []
        caps = self.capabilities

        if ctx.parts and not caps.supports_threads:
            findings.append(
                Finding(
                    code="threads_not_supported",
                    message=f"{caps.display_name} does not support multi-part threads/drips.",
                    field="parts",
                )
            )

        if caps.max_text_length is not None and len(ctx.text) > caps.max_text_length:
            findings.append(
                Finding(
                    code="text_too_long",
                    message=f"{caps.display_name} allows at most {caps.max_text_length} characters "
                    f"(this post has {len(ctx.text)}).",
                    field="text",
                )
            )

        if caps.max_text_length is not None:
            for i, part in enumerate(ctx.parts or []):
                if len(part.text) > caps.max_text_length:
                    findings.append(
                        Finding(
                            code="part_text_too_long",
                            message=f"{caps.display_name} allows at most {caps.max_text_length} characters "
                            f"per part (part {i + 1} has {len(part.text)}).",
                            field="parts",
                        )
                    )

        if ctx.post_kind not in caps.post_kinds:
            findings.append(
                Finding(
                    code="unsupported_post_kind",
                    message=f"{caps.display_name} does not support {ctx.post_kind} posts.",
                    field="post_kind",
                )
            )

        for media_item in ctx.media:
            rule = caps.media_rule_for(media_item.kind)
            if rule is None:
                findings.append(
                    Finding(
                        code="unsupported_media_kind",
                        message=f"{caps.display_name} does not accept {media_item.kind} media.",
                        field="media",
                    )
                )
                continue
            if rule.max_size_mb and media_item.size_bytes:
                size_mb = media_item.size_bytes / (1024 * 1024)
                if size_mb > rule.max_size_mb:
                    findings.append(
                        Finding(
                            code="media_too_large",
                            message=f"{caps.display_name} allows at most {rule.max_size_mb:.0f}MB per "
                            f"{media_item.kind} (this is {size_mb:.1f}MB).",
                            field="media",
                        )
                    )
            if (
                rule.allowed_aspect_ratios
                and media_item.width
                and media_item.height
                and not _matches_any_aspect_ratio(media_item.width, media_item.height, rule.allowed_aspect_ratios)
            ):
                findings.append(
                    Finding(
                        code="unsupported_aspect_ratio",
                        message=f"{caps.display_name} requires one of these aspect ratios for "
                        f"{media_item.kind}: {', '.join(rule.allowed_aspect_ratios)} "
                        f"(this is {media_item.width}x{media_item.height}).",
                        field="media",
                    )
                )
            if rule.max_duration_s and media_item.duration_s and media_item.duration_s > rule.max_duration_s:
                findings.append(
                    Finding(
                        code="media_too_long",
                        message=f"{caps.display_name} allows at most {rule.max_duration_s:.0f}s "
                        f"of video (this is {media_item.duration_s:.0f}s).",
                        field="media",
                    )
                )
            if rule.min_duration_s and media_item.duration_s and media_item.duration_s < rule.min_duration_s:
                findings.append(
                    Finding(
                        code="media_too_short",
                        message=f"{caps.display_name} requires at least {rule.min_duration_s:.0f}s of video.",
                        field="media",
                    )
                )

        counts_by_kind: dict[str, int] = {}
        for media_item in ctx.media:
            counts_by_kind[media_item.kind] = counts_by_kind.get(media_item.kind, 0) + 1
        for kind, count in counts_by_kind.items():
            rule = caps.media_rule_for(kind)
            if rule and count > rule.max_count:
                findings.append(
                    Finding(
                        code="too_many_media_items",
                        message=f"{caps.display_name} accepts at most {rule.max_count} {kind} "
                        f"item(s) per post (this post has {count}).",
                        field="media",
                    )
                )

        return findings

    @abstractmethod
    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        """Advance one step of publishing. Called again with the returned
        state if `done` is False. Must be safe to call more than once with
        the same idempotency_key if a previous call's result was lost (e.g.
        process crash between the HTTP call succeeding and the state being
        persisted) — see each connector's docstring for how it achieves this."""
        raise NotImplementedError

    # --- OAuth (optional: only platforms with 3-legged OAuth implement these) ---
    # `**extra` carries connector-specific inputs the generic OAuth start
    # endpoint doesn't know about — e.g. Mastodon needs the user's instance
    # domain before it can even register an app to get a client_id.

    def authorize_url(self, redirect_uri: str, state: str, **extra: str) -> OAuthAuthorization:
        """`state` is an opaque token the caller wants echoed back unchanged.
        A connector that needs its own bookkeeping across the redirect (e.g.
        Mastodon's per-instance client_id/secret) folds `state` into
        whatever it returns as OAuthAuthorization.state — see
        unwrap_caller_state, which is how the caller gets it back."""
        raise NotImplementedError(f"{self.slug} does not support OAuth")

    def exchange_code(self, code: str, redirect_uri: str, **extra: str) -> ChannelCredentials:
        raise NotImplementedError(f"{self.slug} does not support OAuth")

    def unwrap_caller_state(self, provider_state: str) -> str:
        """Recovers the original `state` the caller passed into
        authorize_url, from whatever the OAuth provider echoed back as its
        own `state` query parameter. Default: identity (the connector didn't
        need to wrap it)."""
        return provider_state

    def refresh(self, credentials: ChannelCredentials) -> ChannelCredentials:
        """Return refreshed credentials, or raise AuthExpired if the platform
        requires a full re-authorization."""
        return credentials

    def token_expiry(self, credentials: ChannelCredentials) -> datetime | None:
        """When the access token expires, if known. Drives the maintenance
        cron's refresh scheduling (Phase 2)."""
        return None

    def fetch_metrics(self, credentials: ChannelCredentials, remote_id: str) -> dict[str, Any]:
        """Best-effort engagement metrics for a published post (Phase 7).
        jobs/metrics.py reads "likes"/"comments"/"shares"/"impressions" (any
        may be omitted or None if the platform doesn't report it) and stores
        the whole dict verbatim in PostMetric.raw — a connector is free to
        include extra platform-specific fields alongside those four. The
        default (empty dict) means "no metrics available for this platform
        yet", which jobs/metrics.py treats as a no-op, not an error."""
        return {}
