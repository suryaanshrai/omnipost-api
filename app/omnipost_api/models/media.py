"""Uploaded media and its per-platform renditions."""

from __future__ import annotations

from django.db import models

from .accounts import User, Workspace


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

    class Meta:
        ordering = ["-created_at"]

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
