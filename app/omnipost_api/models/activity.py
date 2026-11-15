"""Audit events and user notifications."""

from __future__ import annotations

from django.db import models

from .accounts import User, Workspace
from .channels import Channel


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


class Notification(models.Model):
    """Something a person should know about: a post that failed, a channel
    that needs reconnecting. Shown in the app's bell and, if the user has
    email_notifications on, emailed (jobs/notify.py)."""

    KIND_POST_FAILED = "post_failed"
    KIND_CHANNEL_BROKEN = "channel_broken"
    KIND_PREFLIGHT_FAILED = "preflight_failed"
    KIND_CHOICES = [
        (KIND_POST_FAILED, "Post failed"),
        (KIND_CHANNEL_BROKEN, "Channel needs reconnecting"),
        (KIND_PREFLIGHT_FAILED, "Scheduled post at risk"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=30, choices=KIND_CHOICES)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default="")
    # A path inside the app, e.g. "/app/connections/12".
    link = models.CharField(max_length=300, blank=True, default="")
    read_at = models.DateTimeField(null=True, blank=True)
    emailed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "read_at"])]

    def __str__(self) -> str:
        return f"{self.kind} for {self.user}: {self.title}"


class Webhook(models.Model):
    """POSTs a signed JSON event to a URL when something happens in a
    workspace, for Zapier/n8n/your own code. See jobs/webhooks.py."""

    EVENT_POST_PUBLISHED = "post.published"
    EVENT_POST_FAILED = "post.failed"
    EVENT_CHANNEL_BROKEN = "channel.broken"
    EVENTS = [EVENT_POST_PUBLISHED, EVENT_POST_FAILED, EVENT_CHANNEL_BROKEN]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="webhooks")
    url = models.URLField(max_length=500)
    # Shared secret for the X-OmniPost-Signature HMAC. Shown once on create.
    secret = models.CharField(max_length=64)
    events = models.JSONField(default=list)
    is_active = models.BooleanField(default=True)
    last_status = models.CharField(max_length=100, blank=True, default="")
    last_delivered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Webhook to {self.url}"


class Feed(models.Model):
    """An RSS or Atom feed that turns each new item into a draft for the
    chosen channels (jobs/feeds.py). Drafts wait for a person to send them."""

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="feeds")
    url = models.URLField(max_length=500)
    channels = models.ManyToManyField(Channel, related_name="feeds", blank=True)
    # "{title} {link}" with the item's fields filled in.
    template = models.CharField(max_length=500, default="{title} {link}")
    is_active = models.BooleanField(default=True)
    seen_ids = models.JSONField(default=list, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Feed {self.url}"
