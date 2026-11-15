"""AI: voice profiles, provider keys (BYOK), usage counters."""

from __future__ import annotations

from django.db import models

from .. import crypto
from .accounts import Workspace


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
