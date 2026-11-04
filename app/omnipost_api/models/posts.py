"""Posts, their per-channel targets and thread parts, publish attempts and metrics."""

from __future__ import annotations

from django.db import models

from .accounts import User, Workspace
from .channels import Channel
from .media import MediaAsset


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
        ordering = ["-created_at"]

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
        ordering = ["-created_at"]

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
        ordering = ["-created_at"]

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
