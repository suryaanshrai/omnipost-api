"""Shared pieces for the API views: workspace scoping and role checks, query-param and date parsing."""

from __future__ import annotations

from datetime import UTC, datetime

from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import status, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import SAFE_METHODS, IsAuthenticated
from rest_framework.response import Response

from ..models import (
    Membership,
)
from ..permissions import (
    require_role,
    workspace_id_of,
    workspaces_for_user,
)

WORKSPACE_FILTER = OpenApiParameter(
    "workspace", int, required=False, description="Only rows belonging to this workspace (must be one of yours)."
)


def _int_param(request, name: str) -> int | None:
    """An optional integer query param; a non-integer value is a 400, not a silent no-op."""
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValidationError({name: "Must be an integer."}) from None


def _workspace_list(*extra: OpenApiParameter):
    return extend_schema_view(list=extend_schema(parameters=[WORKSPACE_FILTER, *extra]))


class WorkspaceScopedViewSet(viewsets.ModelViewSet):
    """Base for any viewset whose rows belong to a workspace: scopes the
    queryset to workspaces the requesting user is a member of, and requires
    `write_role` (or the action's entry in `action_roles`) in the row's
    workspace for every write — create, update, delete and POST actions."""

    permission_classes = [IsAuthenticated]
    workspace_field = "workspace_id"
    write_role = Membership.ROLE_EDITOR
    action_roles: dict[str, str] = {}

    def required_role(self) -> str:
        return self.action_roles.get(self.action, self.write_role)

    def check_object_permissions(self, request, obj):
        super().check_object_permissions(request, obj)
        if request.method not in SAFE_METHODS:
            require_role(request.user, workspace_id_of(obj), self.required_role())

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.filter(**{f"{self.workspace_field}__in": workspaces_for_user(self.request.user)})
        # `?workspace=<id>` narrows *within* the user's own workspaces — the
        # membership scope above always applies first, so passing someone
        # else's workspace id just yields an empty list.
        workspace_id = _int_param(self.request, "workspace")
        if workspace_id is not None:
            qs = qs.filter(**{self.workspace_field: workspace_id})
        return qs

    def _create_workspace_id(self, data) -> int:
        if data.get("workspace") is not None:
            return data["workspace"].pk
        for key in ("channel", "post"):
            owner = workspace_id_of(data[key]) if data.get(key) is not None else None
            if owner is not None:
                return owner
        raise ValidationError({"workspace": "This field is required."})

    def perform_create(self, serializer):
        require_role(self.request.user, self._create_workspace_id(serializer.validated_data), self.required_role())
        serializer.save()


def _platform_unreachable() -> Response:
    return Response(
        {"detail": "Couldn't reach the platform to finish this step. Try again in a moment."},
        status=status.HTTP_502_BAD_GATEWAY,
    )


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise ValidationError(f"{value!r} isn't a date and time (use ISO 8601).") from None
    if timezone.is_naive(parsed):
        # The old CreatePostView had exactly this gap: a schedule string with
        # no UTC offset produced a naive datetime, and comparing it against
        # an aware one raised TypeError at request time.
        parsed = timezone.make_aware(parsed, UTC)
    return parsed
