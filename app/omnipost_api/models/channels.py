"""Connected accounts and their encrypted credentials, and per-workspace platform apps."""

from __future__ import annotations

from django.db import models

from .. import crypto
from .accounts import User, Workspace


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
        ordering = ["-created_at"]

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
        credential, _ = Credential.objects.update_or_create(channel=self, defaults={"encrypted_values": encrypted})
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
