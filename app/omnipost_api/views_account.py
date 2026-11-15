"""The signed-in person's own things: notifications, data export, and
deleting the account."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth import NoPersonalToken
from .models import Channel, Membership, Notification, Post, Workspace


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = ["id", "workspace", "kind", "title", "body", "link", "read_at", "created_at"]
        read_only_fields = fields


@extend_schema(
    parameters=[OpenApiParameter("unread", bool, required=False, description="Only unread notifications.")]
)
class NotificationViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = Notification.objects.filter(user=self.request.user)
        if self.request.query_params.get("unread") in ("1", "true"):
            qs = qs.filter(read_at__isnull=True)
        return qs

    @extend_schema(request=None, responses=NotificationSerializer)
    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        notification = self.get_queryset().filter(pk=pk).first()
        if notification is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at"])
        return Response(NotificationSerializer(notification).data)

    @extend_schema(request=None, responses=inline_serializer("ReadAll", {"updated": serializers.IntegerField()}))
    @action(detail=False, methods=["post"], url_path="read-all")
    def read_all(self, request):
        updated = self.get_queryset().filter(read_at__isnull=True).update(read_at=timezone.now())
        return Response({"updated": updated})

    @extend_schema(responses=inline_serializer("UnreadCount", {"unread": serializers.IntegerField()}))
    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        return Response({"unread": Notification.objects.filter(user=request.user, read_at__isnull=True).count()})


class AccountExportView(APIView):
    """GET -> everything OmniPost stores about you and your workspaces, as
    JSON (no credentials or keys, which stay encrypted)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=inline_serializer("AccountExport", {"user": serializers.DictField()}))
    def get(self, request):
        user = request.user
        workspaces = Workspace.objects.filter(memberships__user=user).distinct()
        data = {
            "exported_at": timezone.now().isoformat(),
            "user": {
                "username": user.username,
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "date_joined": user.date_joined.isoformat(),
            },
            "workspaces": [
                {
                    "name": ws.name,
                    "timezone": ws.timezone,
                    "role": ws.memberships.get(user=user).role,
                    "channels": list(
                        Channel.objects.filter(workspace=ws).values("display_name", "connector_slug", "created_at")
                    ),
                    "posts": [
                        {
                            "kind": post.kind,
                            "text": post.base_text,
                            "link": post.link,
                            "status": post.status,
                            "scheduled_for": post.scheduled_for,
                            "created_at": post.created_at,
                            "targets": list(
                                post.targets.values("channel__display_name", "status", "permalink", "published_at")
                            ),
                        }
                        for post in Post.objects.filter(workspace=ws).prefetch_related("targets")
                    ],
                }
                for ws in workspaces
            ],
        }
        response = Response(data)
        response["Content-Disposition"] = 'attachment; filename="omnipost-export.json"'
        return response


class AccountDeleteView(APIView):
    """POST {password} -> deletes your account. Workspaces where you're the
    only member go with it (channels, posts, media). If you own a workspace
    that has other members, make one of them an owner first."""

    permission_classes = [IsAuthenticated, NoPersonalToken]

    @extend_schema(
        request=inline_serializer("AccountDelete", {"password": serializers.CharField()}),
        responses={204: None},
    )
    def post(self, request):
        user = request.user
        if not user.check_password(request.data.get("password") or ""):
            raise ValidationError({"password": "That password isn't right."})

        blocking = []
        for membership in Membership.objects.filter(user=user, role=Membership.ROLE_OWNER).select_related("workspace"):
            others = Membership.objects.filter(workspace=membership.workspace).exclude(user=user)
            if others.exists() and not others.filter(role=Membership.ROLE_OWNER).exists():
                blocking.append(membership.workspace.name)
        if blocking:
            raise ValidationError(
                {"detail": f"Make someone else an owner of {', '.join(blocking)} first, or remove its other members."}
            )

        with transaction.atomic():
            solo = [
                m.workspace
                for m in Membership.objects.filter(user=user).select_related("workspace")
                if not Membership.objects.filter(workspace=m.workspace).exclude(user=user).exists()
            ]
            for workspace in solo:
                organization = workspace.organization
                workspace.delete()
                if not organization.workspaces.exists():
                    organization.delete()
            user.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
