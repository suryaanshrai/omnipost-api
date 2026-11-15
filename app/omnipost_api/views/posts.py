"""Posts, their per-channel targets, and publish history."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view, inline_serializer
from rest_framework import serializers as drf_serializers
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..models import (
    Channel,
    Membership,
    Post,
    PostTarget,
    PublishAttempt,
)
from ..permissions import (
    has_role,
    require_role,
    role_in,
    workspaces_for_user,
)
from ..serializers import (
    CalendarEntrySerializer,
    PostMetricSerializer,
    PostSerializer,
    PostTargetSerializer,
    PublishAttemptSerializer,
)
from ..services.billing import PlanLimitReached
from .common import (
    WorkspaceScopedViewSet,
    _int_param,
    _parse_iso,
    _workspace_list,
)

_MAX_CALENDAR_DAYS = 62


@_workspace_list(OpenApiParameter("post", int, required=False, description="Only this post's targets."))
class PostTargetViewSet(WorkspaceScopedViewSet):
    serializer_class = PostTargetSerializer
    queryset = PostTarget.objects.all()
    workspace_field = "post__workspace_id"
    http_method_names = ["get", "head", "options"]  # mutated only via Post actions

    def get_queryset(self):
        qs = super().get_queryset()
        post_id = _int_param(self.request, "post")
        if post_id is not None:
            qs = qs.filter(post_id=post_id)
        return qs

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


@extend_schema_view(
    list=extend_schema(
        parameters=[
            OpenApiParameter("post", int, required=False, description="Only attempts for this post's targets."),
            OpenApiParameter("post_target", int, required=False, description="Only attempts for this target."),
        ]
    )
)
class PublishAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only publish history. `?post=` backs the Posts screen's
    per-post delivery log (one request per expanded post, instead of
    paging through every attempt in the workspace to match target ids
    client-side); `?post_target=` narrows to one target."""

    serializer_class = PublishAttemptSerializer
    permission_classes = [IsAuthenticated]
    queryset = PublishAttempt.objects.all()

    def get_queryset(self):
        qs = super().get_queryset().filter(post_target__post__workspace_id__in=workspaces_for_user(self.request.user))
        post_id = _int_param(self.request, "post")
        if post_id is not None:
            qs = qs.filter(post_target__post_id=post_id)
        target_id = _int_param(self.request, "post_target")
        if target_id is not None:
            qs = qs.filter(post_target_id=target_id)
        return qs


@_workspace_list(
    OpenApiParameter("status", str, required=False, description="Comma-separated statuses, e.g. `draft,failed`."),
    OpenApiParameter(
        "scheduled",
        str,
        required=False,
        enum=["due", "upcoming"],
        description="Keep only scheduled posts whose time has come (`due`) or not yet (`upcoming`); "
        "posts in other statuses are unaffected.",
    ),
)
class PostViewSet(WorkspaceScopedViewSet):
    serializer_class = PostSerializer
    queryset = Post.objects.prefetch_related(
        "targets__channel", "targets__media", "targets__parts__media", "base_media"
    )
    # Contributors draft and submit; publishing decisions need an editor.
    write_role = Membership.ROLE_CONTRIBUTOR
    action_roles = {
        "destroy": Membership.ROLE_EDITOR,
        "schedule": Membership.ROLE_EDITOR,
        "queue": Membership.ROLE_EDITOR,
        "cancel": Membership.ROLE_EDITOR,
        "retry_target": Membership.ROLE_EDITOR,
        # client_reviewer ranks with viewer; approve/request_changes check
        # for it explicitly.
        "duplicate": Membership.ROLE_CONTRIBUTOR,
        "repost": Membership.ROLE_EDITOR,
        "approve": Membership.ROLE_VIEWER,
        "request_changes": Membership.ROLE_VIEWER,
    }

    def _check_can_review(self, post: Post) -> None:
        role = role_in(self.request.user, post.workspace_id)
        if self.request.user.is_superuser or role == Membership.ROLE_CLIENT_REVIEWER:
            return
        if not has_role(self.request.user, post.workspace_id, Membership.ROLE_EDITOR):
            raise PermissionDenied("Reviewing posts needs the editor or client reviewer role.")

    def get_queryset(self):
        # No django-filter in this project (see WorkspaceScopedViewSet) — just
        # enough ad-hoc filtering that the frontend's Drafts/Posts split isn't
        # forced to fetch every post in the workspace and filter client-side.
        qs = super().get_queryset()
        # (`?workspace=` is handled by WorkspaceScopedViewSet.)
        status_param = self.request.query_params.get("status")
        if status_param:
            statuses = [s.strip() for s in status_param.split(",") if s.strip()]
            if statuses:
                qs = qs.filter(status__in=statuses)
        search = (self.request.query_params.get("q") or "").strip()
        if search:
            qs = qs.filter(base_text__icontains=search)
        scheduled = self.request.query_params.get("scheduled")
        if scheduled in ("due", "upcoming"):
            from django.db.models import Q

            now = timezone.now()
            not_yet = Q(status=Post.STATUS_SCHEDULED, scheduled_for__gt=now)
            qs = (
                qs.exclude(not_yet)
                if scheduled == "due"
                else qs.exclude(Q(status=Post.STATUS_SCHEDULED) & ~Q(scheduled_for__gt=now))
            )
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
        request=inline_serializer("ScheduleRequest", fields={"run_at": drf_serializers.DateTimeField(required=False)}),
        responses=PostSerializer,
    )
    @action(detail=True, methods=["post"])
    def schedule(self, request, pk=None):
        """Schedules every target on this post to the same run_at. `run_at`
        (ISO 8601, optional) overrides Post.scheduled_for for an immediate
        "publish now". For per-channel queue-slot scheduling instead, see
        `queue`."""
        from ..services.scheduling import NothingToSchedule, schedule_post

        post = self.get_object()
        self._check_can_schedule(post)

        run_at_raw = request.data.get("run_at")
        run_at = _parse_iso(run_at_raw) if run_at_raw else (post.scheduled_for or timezone.now())
        try:
            schedule_post(post, run_at)
        except NothingToSchedule as exc:
            raise ValidationError(str(exc)) from exc
        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def queue(self, request, pk=None):
        """Schedules each target via its own channel's next open QueueSlot
        (in that channel's timezone, respecting BlackoutWindows and
        min_gap_minutes) instead of one explicit run_at for every target."""
        from ..services.queueing import NoOpenSlotError
        from ..services.scheduling import NothingToSchedule, queue_post

        post = self.get_object()
        self._check_can_schedule(post)
        try:
            queue_post(post)
        except (NothingToSchedule, NoOpenSlotError) as exc:
            raise ValidationError(str(exc)) from exc
        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)

    @extend_schema(request=None, responses=PostSerializer)
    @action(detail=True, methods=["post"])
    def duplicate(self, request, pk=None):
        """A new draft with the same text, media, link and channels."""
        from ..services.scheduling import duplicate_post

        copy = duplicate_post(self.get_object(), request.user)
        return Response(PostSerializer(copy).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request=inline_serializer(
            "RepostRequest", fields={"when": drf_serializers.ChoiceField(choices=["queue", "now"], default="queue")}
        ),
        responses=PostSerializer,
    )
    @action(detail=True, methods=["post"])
    def repost(self, request, pk=None):
        """Evergreen: copy a post and send it again, into each channel's next
        queue slot (default) or now. With approvals on, the copy goes to
        review instead."""
        from ..services.queueing import NoOpenSlotError
        from ..services.scheduling import NothingToSchedule, duplicate_post, queue_post, schedule_post

        source = self.get_object()
        copy = duplicate_post(source, request.user)
        if source.workspace.approval_workflow_enabled:
            copy.status = Post.STATUS_IN_REVIEW
            copy.save(update_fields=["status", "updated_at"])
            return Response(PostSerializer(copy).data, status=status.HTTP_201_CREATED)
        try:
            if request.data.get("when") == "now":
                schedule_post(copy)
            else:
                queue_post(copy)
        except (NothingToSchedule, NoOpenSlotError) as exc:
            copy.delete()
            raise ValidationError(str(exc)) from exc
        except PlanLimitReached:
            copy.delete()
            raise
        return Response(PostSerializer(copy).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        parameters=[
            OpenApiParameter("workspace", int, required=True),
            OpenApiParameter("ids", str, required=True, description="Comma-separated post ids (up to 100)."),
        ],
        responses=inline_serializer(
            "PerformanceBatch",
            fields={
                "results": drf_serializers.DictField(child=drf_serializers.ListField(child=drf_serializers.DictField()))
            },
        ),
    )
    @action(detail=False, methods=["get"], url_path="performance")
    def performance_batch(self, request):
        """Latest metrics for many posts at once: {post_id: [entry, ...]},
        one entry per published target that has a snapshot. Replaces one
        request per post on the Analytics screen."""
        workspace_id = _int_param(request, "workspace")
        if workspace_id is None or workspace_id not in workspaces_for_user(request.user):
            raise PermissionDenied("You are not a member of this workspace.")
        try:
            ids = [int(x) for x in (request.query_params.get("ids") or "").split(",") if x.strip()][:100]
        except ValueError:
            raise ValidationError({"ids": "Comma-separated integers."}) from None
        targets = (
            PostTarget.objects.filter(post__workspace_id=workspace_id, post_id__in=ids)
            .select_related("channel")
            .prefetch_related("metrics")
        )
        results: dict[str, list] = {str(i): [] for i in ids}
        for target in targets:
            latest = target.metrics.first()
            if latest is None:
                continue
            results[str(target.post_id)].append(
                {
                    "post_target": target.pk,
                    "channel": target.channel_id,
                    "connector_slug": target.channel.connector_slug,
                    "permalink": target.permalink,
                    "metric": PostMetricSerializer(latest).data,
                }
            )
        return Response({"results": results})

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
        self._check_can_review(post)
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
        self._check_can_review(post)
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
        responses=CalendarEntrySerializer(many=True),
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
        if _int_param(request, "workspace") not in workspaces_for_user(request.user):
            raise PermissionDenied("You are not a member of this workspace.")

        start = _parse_iso(start_raw)
        end = _parse_iso(end_raw)
        if end <= start or end - start > timedelta(days=_MAX_CALENDAR_DAYS):
            raise ValidationError(f"end must be after start, and at most {_MAX_CALENDAR_DAYS} days later.")
        targets = (
            PostTarget.objects.filter(post__workspace_id=workspace_id, run_at__gte=start, run_at__lt=end)
            .exclude(status=PostTarget.STATUS_CANCELED)
            .select_related("post", "channel")
            .order_by("run_at")
        )
        return Response(CalendarEntrySerializer(targets, many=True).data)

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
        from ..services.queueing import NoOpenSlotError
        from ..services.scheduling import queue_post, schedule_post

        try:
            workspace_id = int(request.data.get("workspace") or 0)
        except (TypeError, ValueError):
            raise ValidationError({"workspace": "Must be an integer."}) from None
        require_role(request.user, workspace_id, Membership.ROLE_EDITOR)

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
                run_at = _parse_iso(run_at_raw) if run_at_raw else None
                kind = (row.get("kind") or "text").strip() or "text"
                if kind not in dict(Post.KIND_CHOICES):
                    raise ValidationError(f"Unknown kind {kind!r}.")

                # One row, one transaction: a row that fails leaves nothing behind.
                with transaction.atomic():
                    post = Post.objects.create(
                        workspace_id=workspace_id,
                        author=request.user,
                        kind=kind,
                        base_text=text,
                        link=(row.get("link") or "").strip(),
                        status=Post.STATUS_DRAFT,
                    )
                    PostTarget.objects.create(post=post, channel=channel)
                    if run_at:
                        schedule_post(post, run_at)
                    else:
                        queue_post(post)
                created += 1
            except (ValidationError, NoOpenSlotError, PlanLimitReached) as exc:
                detail = exc.detail if isinstance(exc, ValidationError | PlanLimitReached) else str(exc)
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
        """Cancels every target that hasn't started publishing. Bumping the
        epoch makes any queued attempt a no-op when a worker reaches it."""
        from django.db.models import F

        post = self.get_object()
        post.targets.filter(status__in=[PostTarget.STATUS_PENDING, PostTarget.STATUS_SCHEDULED]).update(
            status=PostTarget.STATUS_CANCELED, epoch=F("epoch") + 1
        )
        post.status = Post.STATUS_CANCELED
        post.save(update_fields=["status", "updated_at"])
        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)
