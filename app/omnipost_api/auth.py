"""Auth views around dj-rest-auth: throttled sign-in, signup and reset.
Expiring tokens live in authentication.py, the reset serializer in
auth_serializers.py (both are loaded by DRF/dj-rest-auth settings, so they
can't import these views)."""

from __future__ import annotations

from dj_rest_auth.registration.views import RegisterView
from dj_rest_auth.serializers import PasswordResetSerializer, TokenSerializer
from dj_rest_auth.views import LoginView, PasswordResetConfirmView, PasswordResetView
from drf_spectacular.contrib.rest_auth import RestAuthDetailSerializer as _Detail
from drf_spectacular.utils import extend_schema

# drf-spectacular's dj-rest-auth support matches the original view classes
# only, so the response shapes are declared here for the subclasses.


@extend_schema(responses=TokenSerializer)
class ThrottledLoginView(LoginView):
    throttle_scope = "auth"


@extend_schema(responses={201: TokenSerializer})
class ThrottledRegisterView(RegisterView):
    throttle_scope = "auth"


@extend_schema(request=PasswordResetSerializer, responses=_Detail)
class ThrottledPasswordResetView(PasswordResetView):
    throttle_scope = "auth"


@extend_schema(responses=_Detail)
class ThrottledPasswordResetConfirmView(PasswordResetConfirmView):
    throttle_scope = "auth"
