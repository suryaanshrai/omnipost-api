"""Expiring sign-in tokens. Kept free of view imports: DRF imports this
module while it is still loading its own views."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.exceptions import AuthenticationFailed


def _expired(token) -> bool:
    ttl_days = settings.AUTH_TOKEN_TTL_DAYS
    return bool(ttl_days) and token.created < timezone.now() - timedelta(days=ttl_days)


class ExpiringTokenAuthentication(TokenAuthentication):
    """DRF tokens never expire on their own, so a leaked token worked forever.
    Tokens older than AUTH_TOKEN_TTL_DAYS are deleted and rejected; signing
    in again issues a fresh one."""

    def authenticate_credentials(self, key):
        user, token = super().authenticate_credentials(key)
        if _expired(token):
            token.delete()
            raise AuthenticationFailed("Your session expired. Sign in again.")
        return user, token


def create_token(token_model, user, serializer):
    """dj-rest-auth TOKEN_CREATOR: reuses a live token, replaces an expired one."""
    token = token_model.objects.filter(user=user).first()
    if token is not None and _expired(token):
        token.delete()
        token = None
    if token is None:
        token = token_model.objects.create(user=user)
    return token
