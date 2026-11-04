"""Audit events and user notifications."""

from __future__ import annotations

from django.db import models

from .accounts import User, Workspace


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
