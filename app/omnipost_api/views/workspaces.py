"""Workspaces and their members."""

from __future__ import annotations

from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated

from ..models import (
    Membership,
    Workspace,
)
from ..permissions import (
    require_role,
    role_in,
    workspaces_for_user,
)
from ..serializers import (
    MembershipSerializer,
    WorkspaceSerializer,
)
from .common import (
    WorkspaceScopedViewSet,
    _workspace_list,
)


class WorkspaceViewSet(viewsets.ModelViewSet):
    serializer_class = WorkspaceSerializer
    permission_classes = [IsAuthenticated]
    queryset = Workspace.objects.all()

    def get_queryset(self):
        return Workspace.objects.filter(id__in=workspaces_for_user(self.request.user))

    def check_object_permissions(self, request, obj):
        super().check_object_permissions(request, obj)
        if request.method not in SAFE_METHODS:
            min_role = Membership.ROLE_OWNER if self.action == "destroy" else Membership.ROLE_ADMIN
            require_role(request.user, obj.pk, min_role)

    def perform_create(self, serializer):
        from ..models import Organization
        from ..services.provisioning import unique_workspace_slug

        serializer.validated_data["organization"] = Organization.objects.create(name=serializer.validated_data["name"])
        if not serializer.validated_data.get("slug"):
            serializer.validated_data["slug"] = unique_workspace_slug(serializer.validated_data["name"])
        workspace = serializer.save()
        Membership.objects.create(workspace=workspace, user=self.request.user, role=Membership.ROLE_OWNER)


@_workspace_list()
class MembershipViewSet(WorkspaceScopedViewSet):
    """Admins add existing accounts by username or email, change roles and
    remove members. Nobody can grant a role above their own, and a workspace
    always keeps at least one owner."""

    serializer_class = MembershipSerializer
    queryset = Membership.objects.select_related("user")
    write_role = Membership.ROLE_ADMIN

    def _check_grant(self, workspace_id: int, role: str | None) -> None:
        from ..permissions import ROLE_RANK

        if role is None:
            return
        mine = role_in(self.request.user, workspace_id)
        if not self.request.user.is_superuser and (mine is None or ROLE_RANK[role] > ROLE_RANK[mine]):
            raise PermissionDenied("You can't grant a role above your own.")

    def _check_keeps_an_owner(self, membership: Membership, new_role: str | None) -> None:
        if membership.role != Membership.ROLE_OWNER or new_role == Membership.ROLE_OWNER:
            return
        others = Membership.objects.filter(workspace_id=membership.workspace_id, role=Membership.ROLE_OWNER).exclude(
            pk=membership.pk
        )
        if not others.exists():
            raise ValidationError("A workspace needs at least one owner.")

    def perform_create(self, serializer):
        data = serializer.validated_data
        require_role(self.request.user, data["workspace"].pk, self.write_role)
        self._check_grant(data["workspace"].pk, data.get("role", Membership.ROLE_EDITOR))
        serializer.save()

    def perform_update(self, serializer):
        new_role = serializer.validated_data.get("role")
        self._check_grant(serializer.instance.workspace_id, new_role)
        self._check_keeps_an_owner(serializer.instance, new_role)
        serializer.save()

    def perform_destroy(self, instance):
        self._check_keeps_an_owner(instance, None)
        instance.delete()

    def check_object_permissions(self, request, obj):
        # Anyone may leave a workspace (delete their own membership); every
        # other write needs admin.
        if self.action == "destroy" and obj.user_id == request.user.pk:
            return
        super().check_object_permissions(request, obj)
