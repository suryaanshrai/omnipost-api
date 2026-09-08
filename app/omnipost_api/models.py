"""OmniPost's data model.

Replaces the old schema entirely (six near-duplicate post tables — PostText,
PostImage, PostVideo, ShortFormVideo, StoryImage, StoryVideo — each with a
copy-pasted save() method; Platform.config as a hand-edited JSON blob;
PlatformInstance's password-derived credential encryption). There was no
production data to migrate (confirmed with the project owner), so this is a
clean break rather than a data migration.

The new shape is Organization -> Workspace -> Channel, with Post/PostTarget
splitting "the idea" from "what actually goes to each platform" — the
composer can now build one post with five platform-specific variants instead
of cross-posting identical text everywhere, which the old schema had no place
to put (post_configs existed but the UI never wrote to it).
"""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models

from . import crypto


class User(AbstractUser):
    pass


class Organization(models.Model):
    """The billing boundary. A solo signup gets one auto-created Organization
    with one Workspace; an agency adds more Workspaces under the same org."""

    name = models.CharField(max_length=200)
    created_at = models.DateTimeField(auto_now_add=True)

    PLAN_FREE = "free"
    PLAN_PRO = "pro"
    PLAN_AGENCY = "agency"
    PLAN_CHOICES = [(PLAN_FREE, "Free"), (PLAN_PRO, "Pro"), (PLAN_AGENCY, "Agency")]
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default=PLAN_FREE)

    def __str__(self) -> str:
        return self.name


class Workspace(models.Model):
    """A brand or client. Owns channels, media, and posts. Solo users never
    see the org/workspace distinction until they add a second workspace —
    see omnipost_api.services.provisioning.create_personal_workspace."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="workspaces")
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True)
    # IANA name (e.g. "America/New_York"). Drives default scheduling display
    # and queue-slot resolution (Phase 5) — the old system had one global
    # TIME_ZONE = 'Asia/Kolkata' and no per-user concept at all.
    timezone = models.CharField(max_length=64, default="UTC")
    # Off by default: most solo users publish straight from draft. Agencies
    # with a client_reviewer membership role flip this on to require
    # draft -> in_review -> approved before a post can be scheduled/queued.
    approval_workflow_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    ROLE_OWNER = "owner"
    ROLE_ADMIN = "admin"
    ROLE_EDITOR = "editor"
    ROLE_CONTRIBUTOR = "contributor"
    ROLE_VIEWER = "viewer"
    ROLE_CLIENT_REVIEWER = "client_reviewer"
    ROLE_CHOICES = [
        (ROLE_OWNER, "Owner"),
        (ROLE_ADMIN, "Admin"),
        (ROLE_EDITOR, "Editor"),
        (ROLE_CONTRIBUTOR, "Contributor"),
        (ROLE_VIEWER, "Viewer"),
        (ROLE_CLIENT_REVIEWER, "Client reviewer"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default=ROLE_EDITOR)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("workspace", "user")]

    def __str__(self) -> str:
        return f"{self.user} @ {self.workspace} ({self.role})"


class Channel(models.Model):
    """One connected platform account. Replaces PlatformInstance — the
    connector-specific request logic that used to live in Platform.config
    (a hand-edited JSONField) now lives in code under app/connectors/, keyed
    by `connector_slug`."""

    HEALTH_HEALTHY = "healthy"
    HEALTH_NEEDS_ATTENTION = "needs_attention"
    HEALTH_BROKEN = "broken"
    HEALTH_CHOICES = [
        (HEALTH_HEALTHY, "Healthy"),
        (HEALTH_NEEDS_ATTENTION, "Needs attention"),
        (HEALTH_BROKEN, "Broken"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="channels")
    connector_slug = models.CharField(max_length=50)
    display_name = models.CharField(max_length=200)
    external_account_id = models.CharField(max_length=200, blank=True, default="")
    health = models.CharField(max_length=20, choices=HEALTH_CHOICES, default=HEALTH_HEALTHY)
    health_detail = models.CharField(max_length=500, blank=True, default="")
    token_expires_at = models.DateTimeField(null=True, blank=True)
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True)
    # IANA name, or blank to inherit workspace.timezone — see effective_timezone().
    # A client-facing channel (e.g. a UK client's Instagram) often needs its
    # own local queue-slot times even when the workspace itself is elsewhere.
    timezone = models.CharField(max_length=64, blank=True, default="")
    # Minimum spacing enforced by queueing.next_open_slot_run_at between any
    # two non-canceled posts on this channel, regardless of which slots they
    # land in — guards against two slots that are legal individually but too
    # close together (e.g. a 9am and a 9:05am slot on a busy day).
    min_gap_minutes = models.PositiveIntegerField(default=0)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["workspace", "connector_slug"])]

    def __str__(self) -> str:
        return f"{self.display_name} ({self.connector_slug})"

    def effective_timezone(self) -> str:
        return self.timezone or self.workspace.timezone or "UTC"

    def _aad(self) -> bytes:
        # Binds this channel's credentials to this row: a Credential blob
        # copied onto another channel fails to decrypt instead of silently
        # decrypting under the wrong context.
        return f"channel:{self.pk}".encode()

    def get_credentials(self) -> dict[str, str]:
        credential = getattr(self, "credential", None)
        if credential is None:
            return {}
        return crypto.decrypt_dict(credential.encrypted_values, aad=self._aad())

    def set_credentials(self, values: dict[str, str]) -> Credential:
        encrypted = crypto.encrypt_dict(values, aad=self._aad())
        credential, _ = Credential.objects.update_or_create(
            channel=self, defaults={"encrypted_values": encrypted}
        )
        return credential


class Credential(models.Model):
    """Envelope-encrypted secret bag for one Channel. See crypto.py — no user
    password is involved anywhere in this path, which is what makes
    unattended scheduled publishing possible at all."""

    channel = models.OneToOneField(Channel, on_delete=models.CASCADE, related_name="credential")
    encrypted_values = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Credential for {self.channel}"


class MediaAsset(models.Model):
    KIND_IMAGE = "image"
    KIND_VIDEO = "video"
    KIND_GIF = "gif"
    KIND_CHOICES = [(KIND_IMAGE, "Image"), (KIND_VIDEO, "Video"), (KIND_GIF, "GIF")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="media_assets")
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    file = models.FileField(upload_to="media/originals/%Y/%m/")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    mime_type = models.CharField(max_length=100, blank=True, default="")
    size_bytes = models.BigIntegerField(null=True, blank=True)
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    duration_s = models.FloatField(null=True, blank=True)
    alt_text = models.CharField(max_length=1000, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.kind}:{self.file.name}"

    @property
    def aspect_ratio(self) -> str | None:
        if not self.width or not self.height:
            return None
        from math import gcd

        divisor = gcd(self.width, self.height)
        return f"{self.width // divisor}:{self.height // divisor}"


class MediaRendition(models.Model):
    """A per-platform derived variant of a MediaAsset (e.g. a 9:16 crop for
    Reels), cached by (asset, profile) so the same source is never
    re-transcoded twice for the same target shape."""

    asset = models.ForeignKey(MediaAsset, on_delete=models.CASCADE, related_name="renditions")
    profile = models.CharField(max_length=50)  # e.g. "reels_9x16", "shorts_9x16"
    file = models.FileField(upload_to="media/renditions/%Y/%m/")
    width = models.IntegerField(null=True, blank=True)
    height = models.IntegerField(null=True, blank=True)
    duration_s = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("asset", "profile")]


class Post(models.Model):
    """The idea. What actually gets sent to each platform lives on
    PostTarget, which can override text/media/format per channel."""

    STATUS_DRAFT = "draft"
    STATUS_IN_REVIEW = "in_review"
    STATUS_APPROVED = "approved"
    STATUS_SCHEDULED = "scheduled"
    STATUS_PUBLISHING = "publishing"
    STATUS_PUBLISHED = "published"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_DRAFT, "Draft"),
        (STATUS_IN_REVIEW, "In review"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_PUBLISHING, "Publishing"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELED, "Canceled"),
    ]

    KIND_CHOICES = [
        ("text", "Text"),
        ("image", "Image"),
        ("video", "Video"),
        ("story", "Story"),
        ("short_video", "Short video"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="posts")
    author = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="text")
    base_text = models.TextField(blank=True, default="")
    base_media = models.ManyToManyField(MediaAsset, blank=True, related_name="posts")
    link = models.URLField(blank=True, default="")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    scheduled_for = models.DateTimeField(null=True, blank=True)
    ai_generated = models.BooleanField(default=False)
    voice_profile = models.ForeignKey(
        "VoiceProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="posts"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["workspace", "status"])]

    def __str__(self) -> str:
        return f"Post #{self.pk} ({self.status})"


class PostTarget(models.Model):
    """One channel's copy of a Post: its own content override, format,
    schedule, and publish state."""

    STATUS_PENDING = "pending"
    STATUS_SCHEDULED = "scheduled"
    STATUS_PUBLISHING = "publishing"
    STATUS_PUBLISHED = "published"
    STATUS_FAILED = "failed"
    STATUS_CANCELED = "canceled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_SCHEDULED, "Scheduled"),
        (STATUS_PUBLISHING, "Publishing"),
        (STATUS_PUBLISHED, "Published"),
        (STATUS_FAILED, "Failed"),
        (STATUS_CANCELED, "Canceled"),
    ]

    post = models.ForeignKey(Post, on_delete=models.CASCADE, related_name="targets")
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="post_targets")
    format = models.CharField(max_length=20, choices=Post.KIND_CHOICES, blank=True, default="")
    text_override = models.TextField(blank=True, default="")
    media = models.ManyToManyField(MediaAsset, blank=True, related_name="post_targets")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    # Resolved final send time for THIS channel — may differ from Post.scheduled_for
    # once per-channel timezones / queue slots (Phase 5) are applied.
    run_at = models.DateTimeField(null=True, blank=True)
    remote_id = models.CharField(max_length=200, blank=True, default="")
    permalink = models.URLField(blank=True, default="")
    # Set once, at the moment publisher._mark_succeeded records success — unlike
    # updated_at (bumped by any save), this is a stable timestamp for metrics
    # polling windows and best-time-to-post derivation (Phase 7).
    published_at = models.DateTimeField(null=True, blank=True)
    # Bumped only on an explicit user retry/reschedule of an already-terminal
    # target, never by the job engine's own automatic retries — see
    # jobs/publisher.py. Keeps the idempotency key stable across automatic
    # retries of the same logical attempt.
    epoch = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["channel", "status", "run_at"])]

    def __str__(self) -> str:
        return f"PostTarget #{self.pk} -> {self.channel} ({self.status})"

    def effective_text(self) -> str:
        return self.text_override or self.post.base_text

    def effective_media(self) -> list[MediaAsset]:
        overridden = list(self.media.all())
        return overridden if overridden else list(self.post.base_media.all())

    def effective_format(self) -> str:
        return self.format or self.post.kind

    def idempotency_key(self) -> str:
        return f"post-target:{self.pk}:epoch:{self.epoch}"


class PostTargetPart(models.Model):
    """One post in a thread / one delayed drip step under a single
    PostTarget. Only meaningful for a connector whose Capabilities declare
    supports_threads=True (enforced by Connector.validate() in
    connectors/base.py) — a target with no parts publishes as a single post
    exactly as before. A carousel doesn't need this: multiple media items on
    one PostTarget already publish together as one post."""

    post_target = models.ForeignKey(PostTarget, on_delete=models.CASCADE, related_name="parts")
    sequence = models.PositiveIntegerField()
    text = models.TextField(blank=True, default="")
    media = models.ManyToManyField(MediaAsset, blank=True, related_name="post_target_parts")
    # Delay, after this part is posted, before the next part is posted.
    # Ignored on the last part.
    delay_after_s = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = [("post_target", "sequence")]
        ordering = ["sequence"]

    def __str__(self) -> str:
        return f"Part {self.sequence} of {self.post_target}"


class PublishAttempt(models.Model):
    """One try (and its retries within the same epoch) at publishing a
    PostTarget. `state_step`/`state_data` hold a connector's resumable
    PublishState between re-invocations — see app/connectors/base.py and
    jobs/publisher.py. Replaces the old fixed-delay-per-step RQ scheduling,
    where every step fired on schedule whether or not the previous one
    succeeded."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_WAITING = "waiting"  # backing off before a retry
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_RUNNING, "Running"),
        (STATUS_WAITING, "Waiting"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
    ]

    post_target = models.ForeignKey(PostTarget, on_delete=models.CASCADE, related_name="attempts")
    epoch = models.PositiveIntegerField(default=1)
    attempt_number = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    error_class = models.CharField(max_length=30, blank=True, default="")
    # Safe-for-display detail only (ConnectorError.safe_detail). Raw upstream
    # response bodies (which can contain tokens) are never stored here — they
    # go to logs, which are secret-scrubbed by app.logging_filters.
    error_detail = models.TextField(blank=True, default="")
    state_step = models.CharField(max_length=100, blank=True, default="start")
    state_data = models.JSONField(default=dict, blank=True)
    run_at = models.DateTimeField()
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["status", "run_at"])]
        unique_together = [("post_target", "epoch", "attempt_number")]

    def __str__(self) -> str:
        return f"Attempt #{self.attempt_number} for {self.post_target} ({self.status})"


class PostMetric(models.Model):
    """One snapshot of a published PostTarget's engagement, as reported by
    Connector.fetch_metrics (Phase 7). Append-only rather than a single
    mutable row on PostTarget: keeps a light history (impressions/likes grow
    over a post's life) and makes "latest metric" a simple ordering query
    instead of a field that gets silently overwritten.

    likes/comments/shares/impressions are the cross-platform-normalized
    subset every connector maps its own field names onto; anything a
    connector reports beyond that lives in `raw` unmodified."""

    post_target = models.ForeignKey(PostTarget, on_delete=models.CASCADE, related_name="metrics")
    impressions = models.PositiveIntegerField(null=True, blank=True)
    likes = models.PositiveIntegerField(null=True, blank=True)
    comments = models.PositiveIntegerField(null=True, blank=True)
    shares = models.PositiveIntegerField(null=True, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["post_target", "fetched_at"])]
        ordering = ["-fetched_at"]

    def __str__(self) -> str:
        return f"Metrics for {self.post_target} @ {self.fetched_at}"

    def engagement_total(self) -> int:
        return (self.likes or 0) + (self.comments or 0) + (self.shares or 0)


class VoiceProfile(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="voice_profiles")
    name = models.CharField(max_length=200)
    style_summary = models.TextField(blank=True, default="")
    example_posts = models.JSONField(default=list, blank=True)
    is_default = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.name} ({self.workspace})"


class ProviderKey(models.Model):
    """A BYOK LLM/image-provider API key. Same envelope encryption as channel
    Credentials — see crypto.py."""

    PROVIDER_ANTHROPIC = "anthropic"
    PROVIDER_OPENAI = "openai"
    PROVIDER_GEMINI = "gemini"
    PROVIDER_OPENROUTER = "openrouter"
    PROVIDER_CHOICES = [
        (PROVIDER_ANTHROPIC, "Anthropic"),
        (PROVIDER_OPENAI, "OpenAI"),
        (PROVIDER_GEMINI, "Gemini"),
        (PROVIDER_OPENROUTER, "OpenRouter"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="provider_keys")
    provider = models.CharField(max_length=30, choices=PROVIDER_CHOICES)
    encrypted_key = models.JSONField()
    label = models.CharField(max_length=100, blank=True, default="")
    is_active = models.BooleanField(default=True)
    last_validated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("workspace", "provider")]

    def _aad(self) -> bytes:
        return f"provider-key:{self.pk}".encode()

    def get_key(self) -> str:
        return crypto.decrypt(self.encrypted_key, aad=self._aad())

    def __str__(self) -> str:
        return f"{self.provider} key for {self.workspace}"


class AppCredential(models.Model):
    """A workspace's own OAuth app (client_id/client_secret), registered
    directly with a gated platform by the workspace owner. Exists for
    connectors whose `requires_own_app` is True (LinkedIn, X) — platforms
    where a single OmniPost-managed app either can't clear the platform's
    review tier (LinkedIn's Community Management API needs a registered
    legal entity) or would make OmniPost eat every customer's per-post API
    cost (X charges the calling app per post, more per post with a link).
    Same envelope encryption as channel Credentials and ProviderKey."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="app_credentials")
    connector_slug = models.CharField(max_length=50)
    client_id = models.CharField(max_length=300)
    encrypted_client_secret = models.JSONField()
    label = models.CharField(max_length=100, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("workspace", "connector_slug")]

    def _aad(self) -> bytes:
        return f"app-credential:{self.pk}".encode()

    def get_client_secret(self) -> str:
        return crypto.decrypt(self.encrypted_client_secret, aad=self._aad())

    def __str__(self) -> str:
        return f"{self.connector_slug} app for {self.workspace}"


class UsageCounter(models.Model):
    """AI quota tracking with reserve -> commit -> release so a failed
    provider call never burns a user's free-tier allotment. `period` is
    either "lifetime" (the free-tier trial allotment) or a "YYYY-MM" string
    for a recurring paid-tier quota."""

    FEATURE_AI_TEXT = "ai_text_generation"
    FEATURE_AI_IMAGE = "ai_image_generation"
    FEATURE_CHOICES = [(FEATURE_AI_TEXT, "AI text generation"), (FEATURE_AI_IMAGE, "AI image generation")]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="usage_counters")
    feature = models.CharField(max_length=50, choices=FEATURE_CHOICES)
    period = models.CharField(max_length=20, default="lifetime")
    reserved = models.PositiveIntegerField(default=0)
    committed = models.PositiveIntegerField(default=0)
    limit = models.PositiveIntegerField(null=True, blank=True)  # null = unlimited

    class Meta:
        unique_together = [("workspace", "feature", "period")]

    def __str__(self) -> str:
        return f"{self.feature}[{self.period}] for {self.workspace}: {self.committed}/{self.limit}"


class AuditEvent(models.Model):
    workspace = models.ForeignKey(
        Workspace, on_delete=models.CASCADE, null=True, blank=True, related_name="audit_events"
    )
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    verb = models.CharField(max_length=100)
    target_type = models.CharField(max_length=50, blank=True, default="")
    target_id = models.CharField(max_length=50, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["workspace", "created_at"])]

    def __str__(self) -> str:
        return f"{self.verb} by {self.actor} at {self.created_at}"


class QueueSlot(models.Model):
    """A recurring weekly posting slot for a channel. 'Add to queue' fills
    the next open slot instead of requiring an exact date/time — the old
    composer had a single DateTimePicker and nothing else."""

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="queue_slots")
    weekday = models.PositiveSmallIntegerField()  # 0 = Monday .. 6 = Sunday
    time_of_day = models.TimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = [("channel", "weekday", "time_of_day")]
        ordering = ["weekday", "time_of_day"]

    def __str__(self) -> str:
        return f"{self.channel} slot: day {self.weekday} @ {self.time_of_day}"


class BlackoutWindow(models.Model):
    """An absolute UTC time range during which queueing.next_open_slot_run_at
    will never resolve a run_at — e.g. a client's announced product launch
    window, or a holiday freeze. `channel=None` applies to every channel in
    the workspace; a channel-specific window narrows it to just that one."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="blackout_windows")
    channel = models.ForeignKey(
        Channel, on_delete=models.CASCADE, null=True, blank=True, related_name="blackout_windows"
    )
    label = models.CharField(max_length=200, blank=True, default="")
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    is_active = models.BooleanField(default=True)

    class Meta:
        indexes = [models.Index(fields=["workspace", "starts_at", "ends_at"])]
        ordering = ["starts_at"]

    def __str__(self) -> str:
        return self.label or f"Blackout {self.starts_at} - {self.ends_at}"


class RecurrenceRule(models.Model):
    """An evergreen post: fires on a fixed interval, each time creating a new
    Post+PostTarget from the next text variant in rotation (round-robin, so
    the same three variants don't read as an obvious copy-paste loop) and
    scheduling it immediately. Recognizably the same "Postgres is truth"
    pattern as PublishAttempt/PostTarget.run_at — next_run_at lives here, not
    in Redis, so a flushed queue never loses track of when the next
    occurrence is due. See jobs/recurrence.py."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="recurrence_rules")
    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="recurrence_rules")
    kind = models.CharField(max_length=20, choices=Post.KIND_CHOICES, default="text")
    variants = models.JSONField(default=list, blank=True)
    link = models.URLField(blank=True, default="")
    media = models.ManyToManyField(MediaAsset, blank=True, related_name="recurrence_rules")
    interval_hours = models.PositiveIntegerField()
    next_variant_index = models.PositiveIntegerField(default=0)
    next_run_at = models.DateTimeField()
    end_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["is_active", "next_run_at"])]

    def __str__(self) -> str:
        return f"Recurrence #{self.pk} -> {self.channel} every {self.interval_hours}h"
