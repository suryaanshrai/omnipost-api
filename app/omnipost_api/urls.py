from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views, views_ai

router = DefaultRouter()
router.register("workspaces", views.WorkspaceViewSet, basename="workspace")
router.register("memberships", views.MembershipViewSet, basename="membership")
router.register("channels", views.ChannelViewSet, basename="channel")
router.register("media", views.MediaAssetViewSet, basename="media-asset")
router.register("posts", views.PostViewSet, basename="post")
router.register("post-targets", views.PostTargetViewSet, basename="post-target")
router.register("publish-attempts", views.PublishAttemptViewSet, basename="publish-attempt")
router.register("voice-profiles", views.VoiceProfileViewSet, basename="voice-profile")
router.register("provider-keys", views.ProviderKeyViewSet, basename="provider-key")
router.register("app-credentials", views.AppCredentialViewSet, basename="app-credential")
router.register("queue-slots", views.QueueSlotViewSet, basename="queue-slot")
router.register("blackout-windows", views.BlackoutWindowViewSet, basename="blackout-window")
router.register("recurrence-rules", views.RecurrenceRuleViewSet, basename="recurrence-rule")

app_name = "omnipost_api"

urlpatterns = [
    path("", include(router.urls)),
    path("connectors/", views.ConnectorsView.as_view(), name="connectors"),
    path("oauth/start/", views.OAuthStartView.as_view(), name="oauth-start"),
    path("oauth/complete/", views.OAuthCompleteView.as_view(), name="oauth-complete"),
    path("validate/", views.ValidateView.as_view(), name="validate"),
    path("ai/variants/", views_ai.GenerateVariantsView.as_view(), name="ai-variants"),
    path("ai/repurpose/", views_ai.RepurposeView.as_view(), name="ai-repurpose"),
    path("ai/alt-text/", views_ai.AltTextView.as_view(), name="ai-alt-text"),
    path("ai/images/", views_ai.GenerateImageView.as_view(), name="ai-images"),
    path("ai/usage/", views_ai.UsageView.as_view(), name="ai-usage"),
    path("auth/", include("dj_rest_auth.urls")),
    path("auth/registration/", include("dj_rest_auth.registration.urls")),
]
