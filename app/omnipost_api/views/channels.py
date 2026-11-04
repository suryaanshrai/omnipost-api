"""Connected accounts: channels, the connector catalog, OAuth connect, and pre-publish validation."""

from __future__ import annotations

import requests
from connectors.errors import ConnectorError
from django.core import signing
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import (
    Channel,
    Membership,
)
from ..permissions import (
    require_role,
    workspaces_for_user,
)
from ..serializers import (
    BestTimeSerializer,
    ChannelSerializer,
    ConnectorSerializer,
    ValidateFindingSerializer,
    ValidateRequestSerializer,
)
from .common import (
    WorkspaceScopedViewSet,
    _platform_unreachable,
    _workspace_list,
)


@_workspace_list()
class ChannelViewSet(WorkspaceScopedViewSet):
    serializer_class = ChannelSerializer
    queryset = Channel.objects.all()
    write_role = Membership.ROLE_ADMIN

    def perform_create(self, serializer):
        super().perform_create(serializer)
        serializer.instance.created_by = self.request.user
        serializer.instance.save(update_fields=["created_by"])

    @extend_schema(responses=BestTimeSerializer(many=True))
    @action(detail=True, methods=["get"], url_path="best-times")
    def best_times(self, request, pk=None):
        """Best-time-to-post suggestions derived from this channel's own
        publish + engagement history — see services/best_times.py. Returns
        an empty list (not an error) when there isn't enough history yet."""
        from ..services.best_times import best_times_for_channel

        channel = self.get_object()
        suggestions = best_times_for_channel(channel)
        return Response(
            [
                {
                    "weekday": s.weekday,
                    "hour": s.hour,
                    "sample_size": s.sample_size,
                    "avg_engagement": s.avg_engagement,
                }
                for s in suggestions
            ]
        )


class ConnectorsView(APIView):
    """GET -> every registered connector's capabilities and gating info.
    Previously nowhere for the frontend to read this from at all: the old UI
    hardcoded a 3-platform table (Instagram/Facebook/LinkedIn) that was
    already wrong the day the backend grew to eleven connectors. This is the
    single source of truth `connectors.registry` — same data
    Connector.validate() enforces server-side — so the Connections screen and
    composer can't drift from it the way the hardcoded table did."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=ConnectorSerializer(many=True))
    def get(self, request):
        from connectors import registry

        connectors = []
        for slug in registry.all_slugs():
            connector = registry.get(slug)
            caps = connector.capabilities
            connectors.append(
                {
                    "slug": caps.slug,
                    "display_name": caps.display_name,
                    "requires_own_app": connector.requires_own_app,
                    "supports_oauth": connector.supports_oauth,
                    "available": connector.available_on_server,
                    "supports_metrics": connector.supports_metrics,
                    "supports_first_comment": connector.supports_first_comment,
                    "credential_fields": list(connector.credential_fields),
                    "oauth_extra_fields": list(connector.oauth_extra_fields),
                    "max_text_length": caps.max_text_length,
                    "supports_link": caps.supports_link,
                    "supports_alt_text": caps.supports_alt_text,
                    "supports_threads": caps.supports_threads,
                    "supports_scheduling_native": caps.supports_scheduling_native,
                    "post_kinds": list(caps.post_kinds),
                    "media": [
                        {
                            "kinds": list(rule.kinds),
                            "max_count": rule.max_count,
                            "max_size_mb": rule.max_size_mb,
                            "max_duration_s": rule.max_duration_s,
                            "min_duration_s": rule.min_duration_s,
                            "allowed_aspect_ratios": list(rule.allowed_aspect_ratios),
                            "allowed_mime_types": list(rule.allowed_mime_types),
                        }
                        for rule in caps.media
                    ],
                }
            )
        return Response(ConnectorSerializer(connectors, many=True).data)


class OAuthStartView(APIView):
    """POST {connector_slug, workspace, display_name, redirect_uri, extra}
    -> {authorize_url, provider_state}. The frontend redirects the browser to
    authorize_url, then POSTs to OAuthCompleteView with whatever the provider
    appended to redirect_uri (`code`, `state`)."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=inline_serializer(
            "OAuthStartRequest",
            fields={
                "connector_slug": drf_serializers.CharField(),
                "workspace": drf_serializers.IntegerField(),
                "display_name": drf_serializers.CharField(required=False),
                "redirect_uri": drf_serializers.CharField(),
                "extra": drf_serializers.JSONField(required=False),
            },
        ),
        responses=inline_serializer(
            "OAuthStartResult",
            fields={"authorize_url": drf_serializers.CharField(), "provider_state": drf_serializers.CharField()},
        ),
    )
    def post(self, request):
        from ..services import oauth

        try:
            workspace_id = int(request.data.get("workspace") or 0)
        except (TypeError, ValueError):
            raise ValidationError({"workspace": "Must be an integer."}) from None
        require_role(request.user, workspace_id, Membership.ROLE_ADMIN)

        connector_slug = request.data.get("connector_slug")
        redirect_uri = request.data.get("redirect_uri")
        display_name = request.data.get("display_name") or connector_slug
        if not connector_slug or not redirect_uri:
            raise ValidationError("connector_slug and redirect_uri are required.")

        try:
            result = oauth.start(
                user_id=request.user.pk,
                workspace_id=workspace_id,
                connector_slug=connector_slug,
                display_name=display_name,
                redirect_uri=redirect_uri,
                extra=request.data.get("extra") or {},
            )
        except oauth.MissingAppCredential as exc:
            raise ValidationError(str(exc)) from exc
        except ConnectorError as exc:
            raise ValidationError(exc.safe_detail) from exc
        except requests.RequestException:
            return _platform_unreachable()
        return Response({"authorize_url": result.authorize_url, "provider_state": result.provider_state})


class OAuthCompleteView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=inline_serializer(
            "OAuthCompleteRequest",
            fields={
                "connector_slug": drf_serializers.CharField(),
                "code": drf_serializers.CharField(),
                "redirect_uri": drf_serializers.CharField(),
                "state": drf_serializers.CharField(),
            },
        ),
        responses=ChannelSerializer,
    )
    def post(self, request):
        from ..services import oauth

        connector_slug = request.data.get("connector_slug")
        code = request.data.get("code")
        redirect_uri = request.data.get("redirect_uri")
        provider_state = request.data.get("state")
        if not all([connector_slug, code, redirect_uri, provider_state]):
            raise ValidationError("connector_slug, code, redirect_uri, and state are all required.")

        try:
            channel = oauth.complete(
                connector_slug=connector_slug,
                code=code,
                redirect_uri=redirect_uri,
                provider_state=provider_state,
                authorize=lambda workspace_id: require_role(request.user, workspace_id, Membership.ROLE_ADMIN),
                user_id=request.user.pk,
            )
        except (oauth.MissingAppCredential, oauth.StateMismatch) as exc:
            raise ValidationError(str(exc)) from exc
        except signing.BadSignature as exc:
            # A forged, truncated, or (SignatureExpired, a subclass) stale
            # `state` — an expected outcome of a user returning to an old
            # callback URL, not a server error.
            raise ValidationError(
                "This sign-in expired or wasn't started from OmniPost. Start the connection again."
            ) from exc
        except ConnectorError as exc:
            raise ValidationError(exc.safe_detail) from exc
        except requests.RequestException:
            return _platform_unreachable()

        channel.created_by = channel.created_by or request.user
        channel.save(update_fields=["created_by"])
        return Response(ChannelSerializer(channel).data, status=status.HTTP_201_CREATED)


class ValidateView(APIView):
    """POST {channel, text, post_kind, link, media: [asset_id...]} -> per-
    channel findings from Connector.validate() — the same check the job
    engine runs before publishing, exposed here so the composer can surface
    "this is 4:5, Reels needs 9:16" before anything gets scheduled, not
    after a publish attempt fails."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=ValidateRequestSerializer,
        responses=inline_serializer(
            "ValidateResult",
            fields={
                "channel": drf_serializers.IntegerField(),
                "ok": drf_serializers.BooleanField(),
                "findings": ValidateFindingSerializer(many=True),
            },
        ),
    )
    def post(self, request):
        from connectors.base import ChannelCredentials, PublishContext
        from connectors.registry import get as get_connector
        from jobs.context import media_ref

        payload = ValidateRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        channel = payload.validated_data["channel"]
        if channel.workspace_id not in workspaces_for_user(request.user):
            raise PermissionDenied("You are not a member of this workspace.")

        media_assets = payload.validated_data.get("media") or []
        for asset in media_assets:
            if asset.workspace_id != channel.workspace_id:
                raise ValidationError({"media": "Media must belong to the same workspace as the channel."})

        ctx = PublishContext(
            text=payload.validated_data["text"],
            media=[media_ref(asset) for asset in media_assets],
            post_kind=payload.validated_data["post_kind"],
            credentials=ChannelCredentials(values={}),
            idempotency_key="validate",
            link=payload.validated_data.get("link") or None,
        )
        connector = get_connector(channel.connector_slug)
        findings = connector.validate(ctx)
        return Response(
            {
                "channel": channel.pk,
                "ok": not any(f.blocking for f in findings),
                "findings": [
                    {"code": f.code, "message": f.message, "blocking": f.blocking, "field": f.field} for f in findings
                ],
            }
        )
