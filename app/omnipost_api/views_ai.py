"""AI feature endpoints. Kept separate from views.py because this is a
distinct feature area (Phase 4) with its own error-mapping concerns — every
view here needs to translate ai.errors exceptions into HTTP responses, which
none of the connector/media/scheduling views need to do.
"""

from __future__ import annotations

from ai import service
from ai.errors import AIConfigurationError, AIProviderError
from ai.errors import QuotaExceeded as AIQuotaExceeded
from ai.quota import usage_for
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import MediaAsset, UsageCounter, Workspace
from .permissions import workspaces_for_user
from .serializers import MediaAssetSerializer
from .serializers_ai import (
    AltTextRequestSerializer,
    GenerateImageRequestSerializer,
    GenerateVariantsRequestSerializer,
    RepurposeRequestSerializer,
)


def _require_workspace_access(request, workspace: Workspace) -> None:
    if workspace.pk not in workspaces_for_user(request.user):
        raise PermissionDenied("You are not a member of this workspace.")


def _run(fn, *args, **kwargs) -> Response:
    """Runs an ai.service call and maps its exceptions to HTTP responses.
    Centralised so every AI view handles quota/config/provider failures the
    same way instead of repeating try/except blocks."""
    try:
        return Response(fn(*args, **kwargs))
    except AIQuotaExceeded as exc:
        raise ValidationError({"detail": exc.safe_detail}, code="quota_exceeded") from exc
    except AIConfigurationError as exc:
        raise ValidationError({"detail": exc.safe_detail}, code="not_configured") from exc
    except AIProviderError as exc:
        return Response({"detail": exc.safe_detail}, status=status.HTTP_502_BAD_GATEWAY)


class GenerateVariantsView(APIView):
    """POST {workspace, brief, channels: [id...], voice_profile?, provider?}
    -> {variants: [{channel, text, findings}]}. One brief, one LLM call,
    one native variant per requested channel — the differentiator feature
    from the plan, not N separate "generate a caption" calls."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        payload = GenerateVariantsRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        workspace = payload.validated_data["workspace"]
        _require_workspace_access(request, workspace)

        channels = list(payload.validated_data["channels"])
        for channel in channels:
            if channel.workspace_id != workspace.pk:
                raise ValidationError({"channels": "All channels must belong to the given workspace."})
        voice_profile = payload.validated_data.get("voice_profile")
        if voice_profile and voice_profile.workspace_id != workspace.pk:
            raise ValidationError({"voice_profile": "Must belong to the given workspace."})

        return _run(
            lambda: {
                "variants": service.generate_variants(
                    workspace,
                    brief=payload.validated_data["brief"],
                    channels=channels,
                    voice_profile=voice_profile,
                    provider=payload.validated_data.get("provider"),
                )
            }
        )


class RepurposeView(APIView):
    """POST {workspace, source_text | source_url, target_formats: [str...],
    voice_profile?, provider?} -> {results: {format: text}}."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        payload = RepurposeRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        workspace = payload.validated_data["workspace"]
        _require_workspace_access(request, workspace)

        voice_profile = payload.validated_data.get("voice_profile")
        if voice_profile and voice_profile.workspace_id != workspace.pk:
            raise ValidationError({"voice_profile": "Must belong to the given workspace."})

        return _run(
            lambda: {
                "results": service.repurpose_content(
                    workspace,
                    source_text=payload.validated_data.get("source_text") or None,
                    source_url=payload.validated_data.get("source_url") or None,
                    target_formats=payload.validated_data["target_formats"],
                    voice_profile=voice_profile,
                    provider=payload.validated_data.get("provider"),
                )
            }
        )


class AltTextView(APIView):
    """POST {media, provider?} -> {alt_text}. Also writes the result onto
    MediaAsset.alt_text so the composer picks it up immediately."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        payload = AltTextRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        asset: MediaAsset = payload.validated_data["media"]
        _require_workspace_access(request, asset.workspace)

        provider = payload.validated_data.get("provider")
        return _run(lambda: {"alt_text": service.generate_alt_text(asset.workspace, asset, provider=provider)})


class GenerateImageView(APIView):
    """POST {workspace, prompt, aspect?, provider?} -> the created MediaAsset."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        payload = GenerateImageRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        workspace = payload.validated_data["workspace"]
        _require_workspace_access(request, workspace)

        try:
            asset = service.generate_image_asset(
                workspace,
                prompt=payload.validated_data["prompt"],
                aspect=payload.validated_data.get("aspect") or None,
                uploaded_by=request.user,
                provider=payload.validated_data.get("provider"),
            )
        except AIQuotaExceeded as exc:
            raise ValidationError({"detail": exc.safe_detail}, code="quota_exceeded") from exc
        except AIConfigurationError as exc:
            raise ValidationError({"detail": exc.safe_detail}, code="not_configured") from exc
        except AIProviderError as exc:
            return Response({"detail": exc.safe_detail}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(MediaAssetSerializer(asset).data, status=status.HTTP_201_CREATED)


class UsageView(APIView):
    """GET ?workspace=<id> -> current AI quota usage, for a usage badge."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        workspace_id = request.query_params.get("workspace")
        if not workspace_id:
            raise ValidationError({"workspace": "This query parameter is required."})
        try:
            workspace = Workspace.objects.get(pk=workspace_id)
        except (Workspace.DoesNotExist, ValueError, TypeError):
            raise ValidationError({"workspace": "No such workspace."}) from None
        _require_workspace_access(request, workspace)

        from django.conf import settings

        default_limits = {
            UsageCounter.FEATURE_AI_TEXT: settings.AI_FREE_TEXT_LIMIT,
            UsageCounter.FEATURE_AI_IMAGE: settings.AI_FREE_IMAGE_LIMIT,
        }
        counters = {c.feature: c for c in usage_for(workspace)}
        result = {}
        for feature, _ in UsageCounter.FEATURE_CHOICES:
            counter = counters.get(feature)
            result[feature] = {
                "committed": counter.committed if counter else 0,
                "reserved": counter.reserved if counter else 0,
                "limit": counter.limit if counter else default_limits.get(feature),
            }
        return Response(result)
