"""Per-workspace settings: voice profiles, AI provider keys, platform app credentials."""

from __future__ import annotations

from ..models import (
    AppCredential,
    Membership,
    ProviderKey,
    VoiceProfile,
)
from ..serializers import (
    AppCredentialSerializer,
    ProviderKeySerializer,
    VoiceProfileSerializer,
)
from .common import (
    WorkspaceScopedViewSet,
    _workspace_list,
)


@_workspace_list()
class VoiceProfileViewSet(WorkspaceScopedViewSet):
    serializer_class = VoiceProfileSerializer
    queryset = VoiceProfile.objects.all()


@_workspace_list()
class ProviderKeyViewSet(WorkspaceScopedViewSet):
    serializer_class = ProviderKeySerializer
    queryset = ProviderKey.objects.all()
    write_role = Membership.ROLE_ADMIN


@_workspace_list()
class AppCredentialViewSet(WorkspaceScopedViewSet):
    serializer_class = AppCredentialSerializer
    queryset = AppCredential.objects.all()
    write_role = Membership.ROLE_ADMIN
