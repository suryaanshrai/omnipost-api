from __future__ import annotations

import csv
import io
import uuid
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.files.storage import default_storage
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    AppCredential,
    BlackoutWindow,
    Channel,
    MediaAsset,
    Membership,
    Post,
    PostTarget,
    ProviderKey,
    PublishAttempt,
    QueueSlot,
    RecurrenceRule,
    VoiceProfile,
    Workspace,
)
from .permissions import user_can_access_workspace, workspaces_for_user
from .serializers import (
    AppCredentialSerializer,
    BestTimeSerializer,
    BlackoutWindowSerializer,
    ChannelSerializer,
    ConnectorSerializer,
    MediaAssetSerializer,
    MediaConfirmRequestSerializer,
    MediaPresignRequestSerializer,
    MembershipSerializer,
    PostMetricSerializer,
    PostSerializer,
    PostTargetSerializer,
    ProviderKeySerializer,
    PublishAttemptSerializer,
    QueueSlotSerializer,
    RecurrenceRuleSerializer,
    ValidateFindingSerializer,
    ValidateRequestSerializer,
    VoiceProfileSerializer,
    WorkspaceSerializer,
)


class WorkspaceScopedViewSet(viewsets.ModelViewSet):
    """Base for any viewset whose model has a direct `workspace` FK: scopes
    the queryset to workspaces the requesting user is a member of. The old
    API had no such scoping at all — any authenticated user could read or
    write any other user's rows by id."""

    permission_classes = [IsAuthenticated]
    workspace_field = "workspace_id"

    def get_queryset(self):
        qs = super().get_queryset()
        return qs.filter(**{f"{self.workspace_field}__in": workspaces_for_user(self.request.user)})

    def perform_create(self, serializer):
        workspace = serializer.validated_data.get("workspace")
        if workspace and workspace.pk not in workspaces_for_user(self.request.user):
            raise PermissionDenied("You are not a member of this workspace.")
        serializer.save()


class WorkspaceViewSet(viewsets.ModelViewSet):
    serializer_class = WorkspaceSerializer
    permission_classes = [IsAuthenticated]
    queryset = Workspace.objects.all()

    def get_queryset(self):
        return Workspace.objects.filter(id__in=workspaces_for_user(self.request.user))

    def perform_create(self, serializer):
        from .models import Organization
        from .services.provisioning import unique_workspace_slug

        organization = serializer.validated_data.get("organization")
        if organization is None:
            organization = Organization.objects.create(name=serializer.validated_data["name"])
            serializer.validated_data["organization"] = organization
        if not serializer.validated_data.get("slug"):
            serializer.validated_data["slug"] = unique_workspace_slug(serializer.validated_data["name"])
        workspace = serializer.save()
        Membership.objects.create(workspace=workspace, user=self.request.user, role=Membership.ROLE_OWNER)


class MembershipViewSet(WorkspaceScopedViewSet):
    serializer_class = MembershipSerializer
    queryset = Membership.objects.select_related("user")


class ChannelViewSet(WorkspaceScopedViewSet):
    serializer_class = ChannelSerializer
    queryset = Channel.objects.all()

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
        from .services.best_times import best_times_for_channel

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

    def create(self, request, *args, **kwargs):
        if "file" not in request.FILES and request.data.get("key"):
            if not settings.S3_MEDIA_ENABLED:
                raise ValidationError({"key": "Direct upload confirmation requires S3 storage to be configured."})
            return self._create_from_key(request)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        workspace = serializer.validated_data.get("workspace")
        if workspace and workspace.pk not in workspaces_for_user(self.request.user):
            raise PermissionDenied("You are not a member of this workspace.")
        serializer.save(uploaded_by=self.request.user)
        from jobs.queue import enqueue_probe

        enqueue_probe(serializer.instance.pk)

    def _create_from_key(self, request):
        payload = MediaConfirmRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        workspace = payload.validated_data["workspace"]
        if workspace.pk not in workspaces_for_user(request.user):
            raise PermissionDenied("You are not a member of this workspace.")

        asset = MediaAsset(
            workspace=workspace,
            uploaded_by=request.user,
            kind=payload.validated_data["kind"],
            alt_text=payload.validated_data.get("alt_text", ""),
        )
        # The object already exists in storage at this key (uploaded directly
        # via the presigned POST from presign()) — point the FieldFile at it
        # rather than re-uploading through Django.
        asset.file.name = payload.validated_data["key"]
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
        if workspace.pk not in workspaces_for_user(request.user):
            raise PermissionDenied("You are not a member of this workspace.")

        safe_name = Path(payload.validated_data["filename"]).name
        key = f"media/originals/{uuid.uuid4()}/{safe_name}"
        content_type = payload.validated_data["content_type"]

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


class VoiceProfileViewSet(WorkspaceScopedViewSet):
    serializer_class = VoiceProfileSerializer
    queryset = VoiceProfile.objects.all()


class ProviderKeyViewSet(WorkspaceScopedViewSet):
    serializer_class = ProviderKeySerializer
    queryset = ProviderKey.objects.all()


class AppCredentialViewSet(WorkspaceScopedViewSet):
    serializer_class = AppCredentialSerializer
    queryset = AppCredential.objects.all()


class QueueSlotViewSet(WorkspaceScopedViewSet):
    serializer_class = QueueSlotSerializer
    queryset = QueueSlot.objects.all()
    workspace_field = "channel__workspace_id"


class BlackoutWindowViewSet(WorkspaceScopedViewSet):
    serializer_class = BlackoutWindowSerializer
    queryset = BlackoutWindow.objects.all()


class RecurrenceRuleViewSet(WorkspaceScopedViewSet):
    serializer_class = RecurrenceRuleSerializer
    queryset = RecurrenceRule.objects.all()


class PostTargetViewSet(WorkspaceScopedViewSet):
    serializer_class = PostTargetSerializer
    queryset = PostTarget.objects.all()
    workspace_field = "post__workspace_id"
    http_method_names = ["get", "head", "options"]  # mutated only via Post actions

    @extend_schema(responses=PostMetricSerializer(many=True))
    @action(detail=True, methods=["get"])
    def performance(self, request, pk=None):
        """Full engagement history for this target, newest first — see
        jobs/metrics.py. Empty for a target that isn't published yet, whose
        channel's connector has no fetch_metrics() implementation, or that
        hasn't had a poll run since it was published."""
        post_target = self.get_object()
        metrics = post_target.metrics.all()
        return Response(PostMetricSerializer(metrics, many=True).data)


class PublishAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = PublishAttemptSerializer
    permission_classes = [IsAuthenticated]
    queryset = PublishAttempt.objects.all()

    def get_queryset(self):
        return super().get_queryset().filter(
            post_target__post__workspace_id__in=workspaces_for_user(self.request.user)
        )


class PostViewSet(WorkspaceScopedViewSet):
    serializer_class = PostSerializer
    queryset = Post.objects.prefetch_related("targets__channel")

    def get_queryset(self):
        # No django-filter in this project (see WorkspaceScopedViewSet) — just
        # enough ad-hoc filtering that the frontend's Drafts/Posts split isn't
        # forced to fetch every post in the workspace and filter client-side.
        qs = super().get_queryset()
        status_param = self.request.query_params.get("status")
        if status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            if statuses:
                qs = qs.filter(status__in=statuses)
        workspace_param = self.request.query_params.get("workspace")
        if workspace_param:
            qs = qs.filter(workspace_id=workspace_param)
        return qs

    def perform_create(self, serializer):
        super().perform_create(serializer)
        serializer.instance.author = self.request.user
        serializer.instance.save(update_fields=["author"])

    def _check_can_schedule(self, post: Post) -> None:
        """Shared gate for `schedule` and `queue`. With the workspace's
        approval workflow off (the default), behaves exactly as before:
        draft/failed only. Switched on, a post must have been approved
        first — see submit_for_review/approve/request_changes below."""
        if post.workspace.approval_workflow_enabled:
            allowed = (Post.STATUS_APPROVED, Post.STATUS_FAILED)
        else:
            allowed = (Post.STATUS_DRAFT, Post.STATUS_FAILED)
        if post.status not in allowed:
            raise ValidationError(f"Cannot schedule a post in status '{post.status}'.")

    @extend_schema(
        request=inline_serializer(
            "ScheduleRequest", fields={"run_at": drf_serializers.DateTimeField(required=False)}
        ),
        responses=PostSerializer,
    )
    @action(detail=True, methods=["post"])
    def schedule(self, request, pk=None):
        """Schedules every target on this post to the same run_at. `run_at`
        (ISO 8601, optional) overrides Post.scheduled_for for an immediate
        "publish now". For per-channel queue-slot scheduling instead, see
        `queue`."""
        from jobs.scheduler import schedule_post_target

        post = self.get_object()
        self._check_can_schedule(post)

        run_at_raw = request.data.get("run_at")
        run_at = _parse_iso(run_at_raw) if run_at_raw else (post.scheduled_for or timezone.now())

        targets = list(post.targets.exclude(status=PostTarget.STATUS_CANCELED))
        if not targets:
            raise ValidationError("This post has no channel targets to schedule.")

        for target in targets:
            schedule_post_target(target, run_at=run_at)

        post.status = Post.STATUS_SCHEDULED
        post.scheduled_for = run_at
        post.save(update_fields=["status", "scheduled_for", "updated_at"])
        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def queue(self, request, pk=None):
        """Schedules each target via its own channel's next open QueueSlot
        (in that channel's timezone, respecting BlackoutWindows and
        min_gap_minutes) instead of one explicit run_at for every target."""
        from jobs.scheduler import schedule_post_target

        from .services.queueing import NoOpenSlotError, next_open_slot_run_at

        post = self.get_object()
        self._check_can_schedule(post)

        targets = list(post.targets.exclude(status=PostTarget.STATUS_CANCELED))
        if not targets:
            raise ValidationError("This post has no channel targets to schedule.")

        resolved: list[datetime] = []
        for target in targets:
            try:
                run_at = next_open_slot_run_at(target.channel)
            except NoOpenSlotError as exc:
                raise ValidationError(str(exc)) from exc
            schedule_post_target(target, run_at=run_at)
            resolved.append(run_at)

        post.status = Post.STATUS_SCHEDULED
        post.scheduled_for = min(resolved)
        post.save(update_fields=["status", "scheduled_for", "updated_at"])
        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses=PostSerializer)
    @action(detail=True, methods=["post"], url_path="submit-for-review")
    def submit_for_review(self, request, pk=None):
        post = self.get_object()
        if post.status != Post.STATUS_DRAFT:
            raise ValidationError(f"Cannot submit a post in status '{post.status}' for review.")
        post.status = Post.STATUS_IN_REVIEW
        post.save(update_fields=["status", "updated_at"])
        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        post = self.get_object()
        if post.status != Post.STATUS_IN_REVIEW:
            raise ValidationError(f"Cannot approve a post in status '{post.status}'.")
        post.status = Post.STATUS_APPROVED
        post.save(update_fields=["status", "updated_at"])
        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses=PostSerializer)
    @action(detail=True, methods=["post"], url_path="request-changes")
    def request_changes(self, request, pk=None):
        """Sends an in-review post back to draft for edits."""
        post = self.get_object()
        if post.status != Post.STATUS_IN_REVIEW:
            raise ValidationError(f"Cannot request changes on a post in status '{post.status}'.")
        post.status = Post.STATUS_DRAFT
        post.save(update_fields=["status", "updated_at"])
        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)

    @extend_schema(
        responses=inline_serializer(
            "PostPerformanceEntry",
            fields={
                "post_target": drf_serializers.IntegerField(),
                "channel": drf_serializers.IntegerField(),
                "connector_slug": drf_serializers.CharField(),
                "permalink": drf_serializers.CharField(),
                "metric": PostMetricSerializer(),
            },
            many=True,
        )
    )
    @action(detail=True, methods=["get"])
    def performance(self, request, pk=None):
        """Per-post performance: each published target's latest engagement
        snapshot, keyed by target id. A target with no snapshot yet (not
        published, no fetch_metrics() support, or not polled yet) is simply
        omitted rather than reported as zero engagement."""
        post = self.get_object()
        result = []
        for target in post.targets.select_related("channel").prefetch_related("metrics"):
            latest = target.metrics.first()  # PostMetric.Meta.ordering = ["-fetched_at"]
            if latest is None:
                continue
            result.append(
                {
                    "post_target": target.pk,
                    "channel": target.channel_id,
                    "connector_slug": target.channel.connector_slug,
                    "permalink": target.permalink,
                    "metric": PostMetricSerializer(latest).data,
                }
            )
        return Response(result)

    @extend_schema(
        parameters=[
            OpenApiParameter("workspace", int, required=True),
            OpenApiParameter("start", str, required=True, description="ISO 8601, inclusive"),
            OpenApiParameter("end", str, required=True, description="ISO 8601, exclusive"),
        ],
        responses=PostTargetSerializer(many=True),
    )
    @action(detail=False, methods=["get"])
    def calendar(self, request):
        """GET ?workspace=<id>&start=<iso>&end=<iso> -> every PostTarget in
        that workspace with run_at inside [start, end), for a calendar view.
        Backs the composer's calendar rather than the flat, unfiltered
        /posts/ list."""
        workspace_id = request.query_params.get("workspace")
        start_raw = request.query_params.get("start")
        end_raw = request.query_params.get("end")
        if not workspace_id or not start_raw or not end_raw:
            raise ValidationError("workspace, start, and end are all required.")
        if int(workspace_id) not in workspaces_for_user(request.user):
            raise PermissionDenied("You are not a member of this workspace.")

        start = _parse_iso(start_raw)
        end = _parse_iso(end_raw)
        targets = (
            PostTarget.objects.filter(post__workspace_id=workspace_id, run_at__gte=start, run_at__lt=end)
            .exclude(status=PostTarget.STATUS_CANCELED)
            .select_related("post", "channel")
            .order_by("run_at")
        )
        return Response(PostTargetSerializer(targets, many=True).data)

    @extend_schema(
        request=inline_serializer(
            "ImportCsvRequest",
            fields={
                "workspace": drf_serializers.IntegerField(),
                "file": drf_serializers.FileField(required=False),
                "csv_text": drf_serializers.CharField(required=False),
            },
        ),
        responses=inline_serializer(
            "ImportCsvResult",
            fields={
                "created": drf_serializers.IntegerField(),
                "errors": inline_serializer(
                    "ImportCsvRowError",
                    fields={"row": drf_serializers.IntegerField(), "detail": drf_serializers.JSONField()},
                    many=True,
                ),
            },
        ),
    )
    @action(detail=False, methods=["post"], url_path="import-csv")
    def import_csv(self, request):
        """Bulk-schedules posts from a CSV: columns `text`, `channel_id`, and
        optionally `link`, `kind`, `run_at`. A row with no `run_at` is queued
        via its channel's next open QueueSlot (see `queue`); a row with one
        is scheduled explicitly (see `schedule`). Upload as multipart `file`
        or as a `csv_text` form field; `workspace` is always required."""
        from jobs.scheduler import schedule_post_target

        from .services.queueing import NoOpenSlotError, next_open_slot_run_at

        workspace_id = request.data.get("workspace")
        if not workspace_id or int(workspace_id) not in workspaces_for_user(request.user):
            raise PermissionDenied("You are not a member of this workspace.")

        if "file" in request.FILES:
            raw = request.FILES["file"].read().decode("utf-8-sig")
        else:
            raw = request.data.get("csv_text", "")
        if not raw.strip():
            raise ValidationError("Provide a CSV file (`file`) or `csv_text`.")

        reader = csv.DictReader(io.StringIO(raw))
        required_columns = {"text", "channel_id"}
        if not required_columns.issubset(set(reader.fieldnames or [])):
            raise ValidationError(f"CSV must have at least these columns: {sorted(required_columns)}")

        created = 0
        errors: list[dict] = []
        for row_number, row in enumerate(reader, start=2):  # header is row 1
            try:
                text = (row.get("text") or "").strip()
                channel_id_raw = (row.get("channel_id") or "").strip()
                if not text or not channel_id_raw:
                    raise ValidationError("text and channel_id are required.")
                try:
                    channel = Channel.objects.get(pk=int(channel_id_raw), workspace_id=workspace_id)
                except (Channel.DoesNotExist, ValueError):
                    raise ValidationError(f"No channel {channel_id_raw!r} in this workspace.") from None

                run_at_raw = (row.get("run_at") or "").strip()
                run_at = _parse_iso(run_at_raw) if run_at_raw else next_open_slot_run_at(channel)

                post = Post.objects.create(
                    workspace_id=workspace_id,
                    author=request.user,
                    kind=(row.get("kind") or "text").strip() or "text",
                    base_text=text,
                    link=(row.get("link") or "").strip(),
                    status=Post.STATUS_DRAFT,
                )
                target = PostTarget.objects.create(post=post, channel=channel)
                schedule_post_target(target, run_at=run_at)
                post.status = Post.STATUS_SCHEDULED
                post.scheduled_for = run_at
                post.save(update_fields=["status", "scheduled_for", "updated_at"])
                created += 1
            except (ValidationError, NoOpenSlotError) as exc:
                detail = exc.detail if isinstance(exc, ValidationError) else str(exc)
                errors.append({"row": row_number, "detail": detail})

        return Response({"created": created, "errors": errors}, status=status.HTTP_200_OK)

    @extend_schema(
        request=None,
        parameters=[OpenApiParameter("target_id", int, location=OpenApiParameter.PATH)],
        responses=inline_serializer(
            "RetryTargetResult",
            fields={**PostTargetSerializer().get_fields(), "attempt_id": drf_serializers.IntegerField()},
        ),
    )
    @action(detail=True, methods=["post"], url_path="retry-target/(?P<target_id>[^/.]+)")
    def retry_target(self, request, pk=None, target_id=None):
        """Retries one failed/canceled target with a fresh epoch, so its
        idempotency key differs from the exhausted attempt's."""
        from jobs.scheduler import schedule_post_target

        post = self.get_object()
        try:
            target = post.targets.get(pk=target_id)
        except PostTarget.DoesNotExist:
            raise ValidationError("No such target on this post.") from None

        if target.status not in (PostTarget.STATUS_FAILED, PostTarget.STATUS_CANCELED):
            raise ValidationError(f"Cannot retry a target in status '{target.status}'.")

        target.epoch += 1
        target.save(update_fields=["epoch"])
        attempt = schedule_post_target(target, run_at=timezone.now())
        return Response(PostTargetSerializer(target).data | {"attempt_id": attempt.pk}, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        post = self.get_object()
        post.targets.filter(
            status__in=[PostTarget.STATUS_PENDING, PostTarget.STATUS_SCHEDULED]
        ).update(status=PostTarget.STATUS_CANCELED)
        post.status = Post.STATUS_CANCELED
        post.save(update_fields=["status", "updated_at"])
        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)


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
        from .services import oauth

        workspace_id = request.data.get("workspace")
        if not workspace_id or not user_can_access_workspace(request.user, int(workspace_id)):
            raise PermissionDenied("You are not a member of this workspace.")

        connector_slug = request.data.get("connector_slug")
        redirect_uri = request.data.get("redirect_uri")
        display_name = request.data.get("display_name") or connector_slug
        if not connector_slug or not redirect_uri:
            raise ValidationError("connector_slug and redirect_uri are required.")

        try:
            result = oauth.start(
                workspace_id=int(workspace_id),
                connector_slug=connector_slug,
                display_name=display_name,
                redirect_uri=redirect_uri,
                extra=request.data.get("extra") or {},
            )
        except oauth.MissingAppCredential as exc:
            raise ValidationError(str(exc)) from exc
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
        from .services import oauth

        connector_slug = request.data.get("connector_slug")
        code = request.data.get("code")
        redirect_uri = request.data.get("redirect_uri")
        provider_state = request.data.get("state")
        if not all([connector_slug, code, redirect_uri, provider_state]):
            raise ValidationError("connector_slug, code, redirect_uri, and state are all required.")

        try:
            channel = oauth.complete(
                connector_slug=connector_slug, code=code, redirect_uri=redirect_uri, provider_state=provider_state
            )
        except oauth.MissingAppCredential as exc:
            raise ValidationError(str(exc)) from exc
        if not user_can_access_workspace(request.user, channel.workspace_id):
            # The signed state was valid but doesn't belong to a workspace
            # this user can access — shouldn't happen outside a forged
            # request, since the state was signed by us in OAuthStartView.
            channel.delete()
            raise PermissionDenied("This authorization does not belong to a workspace you can access.")

        channel.created_by = request.user
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
                    {"code": f.code, "message": f.message, "blocking": f.blocking, "field": f.field}
                    for f in findings
                ],
            }
        )


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timezone.is_naive(parsed):
        # The old CreatePostView had exactly this gap: a schedule string with
        # no UTC offset produced a naive datetime, and comparing it against
        # an aware one raised TypeError at request time.
        parsed = timezone.make_aware(parsed, UTC)
    return parsed
