"""Integrations: outbound webhooks and RSS feeds."""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import Feed, Membership, Webhook
from ..serializers import FeedSerializer, WebhookSerializer
from .common import WorkspaceScopedViewSet, _workspace_list


@_workspace_list()
class WebhookViewSet(WorkspaceScopedViewSet):
    """Signed POSTs to your URL on post.published, post.failed and
    channel.broken. The signing secret is shown once, in the create response."""

    serializer_class = WebhookSerializer
    queryset = Webhook.objects.all()
    write_role = Membership.ROLE_ADMIN

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        data = WebhookSerializer(
            serializer.instance, context={**self.get_serializer_context(), "show_secret": True}
        ).data
        return Response(data, status=status.HTTP_201_CREATED)

    @extend_schema(request=None, responses={202: None})
    @action(detail=True, methods=["post"])
    def test(self, request, pk=None):
        """Sends a `ping` event now."""
        from jobs.queue import enqueue_webhook

        hook = self.get_object()
        enqueue_webhook(hook.pk, {"event": "ping", "workspace": hook.workspace_id, "data": {}})
        return Response(status=status.HTTP_202_ACCEPTED)


@_workspace_list()
class FeedViewSet(WorkspaceScopedViewSet):
    """RSS/Atom feeds whose new items become drafts for the chosen channels."""

    serializer_class = FeedSerializer
    queryset = Feed.objects.prefetch_related("channels")
