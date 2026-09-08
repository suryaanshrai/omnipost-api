"""Workspace-scoped access control.

The old API had no object-level permission checks at all beyond (after
Phase 0) DEFAULT_PERMISSION_CLASSES=[IsAuthenticated] — any authenticated
user could read or write any other user's PlatformInstance by guessing an id.
Every queryset here is scoped to workspaces the requesting user actually
belongs to via Membership, and IsWorkspaceMember re-checks that on writes.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from .models import Membership, Workspace


def workspaces_for_user(user) -> list[int]:
    return list(Membership.objects.filter(user=user).values_list("workspace_id", flat=True))


def user_can_access_workspace(user, workspace_id: int) -> bool:
    if user.is_superuser:
        return True
    return Membership.objects.filter(user=user, workspace_id=workspace_id).exists()


class IsWorkspaceMember(BasePermission):
    """Object must have a `.workspace` attribute (directly, or via a related
    object exposing `.workspace_for_permissions`)."""

    def has_object_permission(self, request, view, obj) -> bool:
        workspace = getattr(obj, "workspace", None)
        if workspace is None:
            get_workspace = getattr(obj, "workspace_for_permissions", None)
            workspace = get_workspace() if callable(get_workspace) else None
        if workspace is None:
            return False
        return user_can_access_workspace(request.user, workspace.pk if isinstance(workspace, Workspace) else workspace)
