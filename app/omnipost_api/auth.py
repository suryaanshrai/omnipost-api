"""Auth views around dj-rest-auth: throttled sign-in, signup and reset.
Expiring tokens live in authentication.py, the reset serializer in
auth_serializers.py (both are loaded by DRF/dj-rest-auth settings, so they
can't import these views)."""

from __future__ import annotations

from dj_rest_auth.registration.views import RegisterView
from dj_rest_auth.views import LoginView, PasswordResetConfirmView, PasswordResetView


class ThrottledLoginView(LoginView):
    throttle_scope = "auth"


class ThrottledRegisterView(RegisterView):
    throttle_scope = "auth"


class ThrottledPasswordResetView(PasswordResetView):
    throttle_scope = "auth"


class ThrottledPasswordResetConfirmView(PasswordResetConfirmView):
    throttle_scope = "auth"
