"""New-signup provisioning: every user gets a personal Organization and
Workspace automatically, so a solo creator never has to understand the
org/workspace/membership model to post their first thing. An agency adds
additional Workspaces under the same Organization later — nothing here
prevents that, it just means the first one is free."""

from __future__ import annotations

from django.utils.text import slugify

from ..models import Membership, Organization, Workspace


def unique_workspace_slug(base: str, fallback: str = "workspace") -> str:
    """A Workspace.slug is unique=True with no default — anything that
    creates a Workspace without an explicit slug (signup provisioning below,
    and WorkspaceViewSet.perform_create for a user-initiated "create my
    workspace") needs one derived and guaranteed free."""
    base_slug = slugify(base) or fallback
    slug = base_slug
    suffix = 1
    while Workspace.objects.filter(slug=slug).exists():
        suffix += 1
        slug = f"{base_slug}-{suffix}"
    return slug


def create_personal_workspace(user) -> Workspace:
    org = Organization.objects.create(name=f"{user.username}'s workspace", plan=Organization.PLAN_FREE)
    slug = unique_workspace_slug(user.username, fallback=f"user-{user.pk}")
    workspace = Workspace.objects.create(organization=org, name=org.name, slug=slug)
    Membership.objects.create(workspace=workspace, user=user, role=Membership.ROLE_OWNER)
    return workspace
