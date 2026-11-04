"""Queue slots, blackout windows and recurring posts."""

from __future__ import annotations

from django.db import models

from .accounts import Workspace
from .channels import Channel
from .media import MediaAsset
from .posts import Post


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
