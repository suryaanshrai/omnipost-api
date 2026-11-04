"""Media uploads (multipart, or S3 presign + confirm)."""

from __future__ import annotations

import uuid
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from ..models import (
    MediaAsset,
    Membership,
)
from ..permissions import (
    require_role,
)
from ..serializers import (
    MediaAssetSerializer,
    MediaConfirmRequestSerializer,
    MediaPresignRequestSerializer,
)
from .common import (
    WorkspaceScopedViewSet,
    _workspace_list,
)

_ALLOWED_MEDIA_PREFIXES = ("image/", "video/")


def _presign_prefix(workspace_id: int) -> str:
    return f"media/originals/ws{workspace_id}/"


@_workspace_list()
class MediaAssetViewSet(WorkspaceScopedViewSet):
    """Two ways to get a file into a MediaAsset:
    * multipart POST /api/v1/media/ with a `file` field — always available,
      the only option for self-host without S3 configured.
    * presign() then a confirming POST with `key` instead of `file` — the
      browser uploads straight to S3, the API never sees the bytes. Only
      available when settings.S3_MEDIA_ENABLED.
    Either way, a successful create enqueues jobs.media.probe_asset so
    width/height/duration/mime_type get filled in asynchronously."""

    serializer_class = MediaAssetSerializer
    queryset = MediaAsset.objects.all()
    write_role = Membership.ROLE_CONTRIBUTOR

    def create(self, request, *args, **kwargs):
        if "file" not in request.FILES and request.data.get("key"):
            if not settings.S3_MEDIA_ENABLED:
                raise ValidationError({"key": "Direct upload confirmation requires S3 storage to be configured."})
            return self._create_from_key(request)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        require_role(self.request.user, serializer.validated_data["workspace"].pk, self.write_role)
        upload = serializer.validated_data.get("file")
        content_type = getattr(upload, "content_type", "") or ""
        if content_type and not content_type.startswith(_ALLOWED_MEDIA_PREFIXES):
            raise ValidationError({"file": "Only image and video files can be uploaded."})
        serializer.save(uploaded_by=self.request.user)
        from jobs.queue import enqueue_probe

        enqueue_probe(serializer.instance.pk)

    def _create_from_key(self, request):
        payload = MediaConfirmRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        workspace = payload.validated_data["workspace"]
        require_role(request.user, workspace.pk, self.write_role)
        key = payload.validated_data["key"]
        if not key.startswith(_presign_prefix(workspace.pk)) or ".." in key:
            # Only keys minted by presign() for this workspace: otherwise any
            # object in the bucket (another workspace's upload) could be
            # claimed by guessing its key.
            raise ValidationError({"key": "Unknown upload key for this workspace."})

        asset = MediaAsset(
            workspace=workspace,
            uploaded_by=request.user,
            kind=payload.validated_data["kind"],
            alt_text=payload.validated_data.get("alt_text", ""),
        )
        # The object already exists in storage at this key (uploaded directly
        # via the presigned POST from presign()) — point the FieldFile at it
        # rather than re-uploading through Django.
        asset.file.name = key
        asset.save()

        from jobs.queue import enqueue_probe

        enqueue_probe(asset.pk)
        return Response(self.get_serializer(asset).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=["post"])
    def presign(self, request):
        """Returns a presigned S3 POST for direct browser upload. The client
        then confirms with POST /api/v1/media/ passing `workspace`, `kind`,
        and the returned `key` (no file body) to create the MediaAsset row."""
        if not settings.S3_MEDIA_ENABLED:
            return Response(
                {
                    "detail": "Direct upload isn't configured for this deployment "
                    "(no AWS_STORAGE_BUCKET_NAME). POST the file directly to /api/v1/media/ instead."
                },
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        payload = MediaPresignRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        workspace = payload.validated_data["workspace"]
        require_role(request.user, workspace.pk, self.write_role)

        safe_name = Path(payload.validated_data["filename"]).name
        key = f"{_presign_prefix(workspace.pk)}{uuid.uuid4()}/{safe_name}"
        content_type = payload.validated_data["content_type"]
        if not content_type.startswith(_ALLOWED_MEDIA_PREFIXES):
            raise ValidationError({"content_type": "Only image and video files can be uploaded."})

        client = default_storage.connection.meta.client
        presigned = client.generate_presigned_post(
            Bucket=settings.AWS_STORAGE_BUCKET_NAME,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, 500 * 1024 * 1024],
            ],
            ExpiresIn=600,
        )
        return Response({"upload": presigned, "key": key})
