"""Users, organizations, workspaces and memberships."""

from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    # Email me when a post fails or a channel needs reconnecting.
    email_notifications = models.BooleanField(default=True)
    # Monday summary of the last week's posts (jobs/digest.py).
    weekly_digest = models.BooleanField(default=True)
    last_digest_at = models.DateTimeField(null=True, blank=True)


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
    # Add utm_source/medium/campaign to a post's link at publish time.
    utm_tagging = models.BooleanField(default=False)
    # Public link-in-bio page at /bio/<slug> (off until switched on).
    bio_enabled = models.BooleanField(default=False)
    bio_description = models.CharField(max_length=300, blank=True, default="")
    # [{"label": "...", "url": "https://..."}], shown above recent posts.
    bio_links = models.JSONField(default=list, blank=True)
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


class ApiToken(models.Model):
    """A personal access token for scripts, Zapier/n8n and the MCP server.
    Only a hash is stored: the key is shown once, when it's made. Keys look
    like `op_<prefix>_<secret>`; the prefix finds the row, the hash proves it."""

    user = models.ForeignKey("omnipost_api.User", on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=12, unique=True)
    key_hash = models.CharField(max_length=64)
    # Read-only tokens can GET but not create, change or publish anything.
    read_only = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix})"
