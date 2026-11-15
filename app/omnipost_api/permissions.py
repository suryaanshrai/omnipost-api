"""Workspace-scoped access control.

Every queryset is scoped to workspaces the requesting user belongs to via
Membership, and every write checks the user's role in the workspace the row
belongs to (see `require_role` and WorkspaceScopedViewSet in views.py).
"""

from __future__ import annotations

from rest_framework.exceptions import PermissionDenied

from .models import Membership

# Higher rank = more power. client_reviewer can read and approve, nothing else.
ROLE_RANK = {
    Membership.ROLE_VIEWER: 0,
    Membership.ROLE_CLIENT_REVIEWER: 0,
    Membership.ROLE_CONTRIBUTOR: 1,
    Membership.ROLE_EDITOR: 2,
    Membership.ROLE_ADMIN: 3,
    Membership.ROLE_OWNER: 4,
}


def workspaces_for_user(user) -> list[int]:
    return list(Membership.objects.filter(user=user).values_list("workspace_id", flat=True))


def role_in(user, workspace_id: int) -> str | None:
    return Membership.objects.filter(user=user, workspace_id=workspace_id).values_list("role", flat=True).first()


def user_can_access_workspace(user, workspace_id: int) -> bool:
    if user.is_superuser:
        return True
    return Membership.objects.filter(user=user, workspace_id=workspace_id).exists()


def has_role(user, workspace_id: int, min_role: str) -> bool:
    if user.is_superuser:
        return True
    role = role_in(user, workspace_id)
    return role is not None and ROLE_RANK[role] >= ROLE_RANK[min_role]


def require_role(user, workspace_id: int, min_role: str) -> None:
    """403 unless `user` is a member of the workspace with at least `min_role`."""
    role = None if user.is_superuser else role_in(user, workspace_id)
    if user.is_superuser:
        return
    if role is None:
        raise PermissionDenied("You are not a member of this workspace.")
    if ROLE_RANK[role] < ROLE_RANK[min_role]:
        raise PermissionDenied(f"This needs the {min_role} role or higher in this workspace.")


def workspace_id_of(obj) -> int | None:
    """The workspace a row belongs to, for models without a direct FK."""
    for path in ("workspace_id", "channel.workspace_id", "post.workspace_id", "post_target.post.workspace_id"):
        value = obj
        try:
            for part in path.split("."):
                value = getattr(value, part)
        except AttributeError:
            continue
        if value is not None:
            return value
    return None
