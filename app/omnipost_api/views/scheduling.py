"""Queue slots, blackout windows and recurring posts."""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter

from ..models import (
    BlackoutWindow,
    QueueSlot,
    RecurrenceRule,
)
from ..serializers import (
    BlackoutWindowSerializer,
    QueueSlotSerializer,
    RecurrenceRuleSerializer,
)
from .common import (
    WorkspaceScopedViewSet,
    _int_param,
    _workspace_list,
)


@_workspace_list(OpenApiParameter("channel", int, required=False, description="Only this channel's slots."))
class QueueSlotViewSet(WorkspaceScopedViewSet):
    serializer_class = QueueSlotSerializer
    queryset = QueueSlot.objects.all()
    workspace_field = "channel__workspace_id"

    def get_queryset(self):
        qs = super().get_queryset()
        channel_id = _int_param(self.request, "channel")
        if channel_id is not None:
            qs = qs.filter(channel_id=channel_id)
        return qs


@_workspace_list()
class BlackoutWindowViewSet(WorkspaceScopedViewSet):
    serializer_class = BlackoutWindowSerializer
    queryset = BlackoutWindow.objects.all()


@_workspace_list()
class RecurrenceRuleViewSet(WorkspaceScopedViewSet):
    serializer_class = RecurrenceRuleSerializer
    queryset = RecurrenceRule.objects.all()
