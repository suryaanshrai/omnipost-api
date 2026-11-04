"""Auth views around dj-rest-auth: throttled sign-in, signup and reset, the
web app's sign-in cookie, and personal API tokens. Expiring tokens live in
authentication.py, the reset serializer in auth_serializers.py (both are
loaded by DRF/dj-rest-auth settings, so they can't import these views)."""

from __future__ import annotations

from datetime import timedelta

from dj_rest_auth.registration.views import RegisterView
from dj_rest_auth.serializers import PasswordResetSerializer, TokenSerializer
from dj_rest_auth.views import LoginView, LogoutView, PasswordChangeView, PasswordResetConfirmView, PasswordResetView
from django.conf import settings
from django.contrib.auth import logout as django_logout
from django.utils import timezone
from drf_spectacular.contrib.rest_auth import RestAuthDetailSerializer as _Detail
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import COOKIE_HEADER_META, hash_key, is_personal_token, new_api_key
from .models import ApiToken


class NoPersonalToken(BasePermission):
    """Things only a signed-in person may do: make or revoke API tokens,
    change the password, delete the account. A leaked API token can't be
    used to mint more, or to lock you out."""

    message = "Sign in to do this; API tokens can't."

    def has_permission(self, request, view):
        return not is_personal_token(request)


def _wants_cookie(request) -> bool:
    return bool(request.META.get(COOKIE_HEADER_META))


def set_auth_cookie(response, key: str) -> None:
    ttl_days = settings.AUTH_TOKEN_TTL_DAYS
    response.set_cookie(
        settings.AUTH_COOKIE_NAME,
        key,
        max_age=int(timedelta(days=ttl_days).total_seconds()) if ttl_days else None,
        httponly=True,
        secure=settings.AUTH_COOKIE_SECURE,
        samesite=settings.AUTH_COOKIE_SAMESITE,
        domain=settings.AUTH_COOKIE_DOMAIN,
        path="/",
    )


def _move_key_to_cookie(request, response):
    """The web app (it sends X-OmniPost-Client) gets the session token as an
    httpOnly cookie and an empty `key`, so page scripts never see it. Other
    clients get the key in the body, as before."""
    key = (response.data or {}).get("key") if response.status_code < 300 else None
    if key and _wants_cookie(request):
        set_auth_cookie(response, key)
        response.data["key"] = ""
    return response


# drf-spectacular's dj-rest-auth support matches the original view classes
# only, so the response shapes are declared here for the subclasses.


@extend_schema(responses=TokenSerializer)
class ThrottledLoginView(LoginView):
    throttle_scope = "auth"

    def post(self, request, *args, **kwargs):
        return _move_key_to_cookie(request, super().post(request, *args, **kwargs))


@extend_schema(responses={201: TokenSerializer})
class ThrottledRegisterView(RegisterView):
    throttle_scope = "auth"

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        # allauth signs the new user into a Django session as well; sign-in
        # here is the token (header or cookie) only.
        django_logout(request._request)
        return _move_key_to_cookie(request, response)


@extend_schema(request=PasswordResetSerializer, responses=_Detail)
class ThrottledPasswordResetView(PasswordResetView):
    throttle_scope = "auth"


@extend_schema(responses=_Detail)
class ThrottledPasswordResetConfirmView(PasswordResetConfirmView):
    throttle_scope = "auth"


@extend_schema(request=None, responses=_Detail)
class CookieLogoutView(LogoutView):
    """Signs out: deletes the session token and clears the cookie. With an
    API token it refuses, so a script can't sign you out of the web app;
    revoke the token instead."""

    throttle_scope = "auth"

    def logout(self, request):
        if is_personal_token(request):
            raise PermissionDenied("This is an API token. Revoke it in Settings instead.")
        response = super().logout(request)
        django_logout(request._request)  # any Django session too (e.g. from signup)
        response.delete_cookie(settings.AUTH_COOKIE_NAME, path="/", domain=settings.AUTH_COOKIE_DOMAIN)
        return response


@extend_schema(responses=_Detail)
class GuardedPasswordChangeView(PasswordChangeView):
    permission_classes = [IsAuthenticated, NoPersonalToken]
    throttle_scope = "auth"


class CookieSessionView(APIView):
    """POST, signed in with a session token in the Authorization header ->
    the same session as an httpOnly cookie. Lets the web app move an older
    sign-in out of localStorage without asking for the password again."""

    permission_classes = [IsAuthenticated, NoPersonalToken]
    throttle_scope = "auth"

    @extend_schema(request=None, responses=_Detail)
    def post(self, request):
        if not isinstance(request.auth, Token) or not _wants_cookie(request):
            raise ValidationError({"detail": "Send a session token and the X-OmniPost-Client header."})
        response = Response({"detail": "Signed in with a cookie."})
        set_auth_cookie(response, request.auth.key)
        return response


# --- personal API tokens ------------------------------------------------------------


class ApiTokenSerializer(serializers.ModelSerializer):
    expires_in_days = serializers.IntegerField(
        write_only=True, required=False, allow_null=True, min_value=1, max_value=3650
    )

    class Meta:
        model = ApiToken
        fields = ["id", "name", "prefix", "read_only", "expires_at", "last_used_at", "created_at", "expires_in_days"]
        read_only_fields = ["id", "prefix", "expires_at", "last_used_at", "created_at"]


class CreatedApiTokenSerializer(ApiTokenSerializer):
    key = serializers.CharField(read_only=True, help_text="Shown only in this response.")

    class Meta(ApiTokenSerializer.Meta):
        fields = [*ApiTokenSerializer.Meta.fields, "key"]


MAX_TOKENS_PER_USER = 25


class ApiTokenViewSet(
    mixins.ListModelMixin, mixins.CreateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    """Personal API tokens: `Authorization: Bearer <key>` on any endpoint, with
    your permissions (or read-only). The key is returned once, on create."""

    serializer_class = ApiTokenSerializer
    queryset = ApiToken.objects.none()  # for the schema; get_queryset() scopes to you
    permission_classes = [IsAuthenticated, NoPersonalToken]
    pagination_class = None

    def get_queryset(self):
        return ApiToken.objects.filter(user=self.request.user)

    @extend_schema(responses={201: CreatedApiTokenSerializer})
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if self.get_queryset().count() >= MAX_TOKENS_PER_USER:
            raise ValidationError({"detail": f"You can have up to {MAX_TOKENS_PER_USER} tokens. Revoke one first."})
        days = serializer.validated_data.pop("expires_in_days", None)
        key, prefix = new_api_key()
        token = ApiToken.objects.create(
            user=request.user,
            prefix=prefix,
            key_hash=hash_key(key),
            expires_at=timezone.now() + timedelta(days=days) if days else None,
            **serializer.validated_data,
        )
        token.key = key  # type: ignore[attr-defined]
        return Response(CreatedApiTokenSerializer(token).data, status=status.HTTP_201_CREATED)
