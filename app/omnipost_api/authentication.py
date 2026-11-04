"""Sign-in: expiring session tokens (header or httpOnly cookie) and
personal API tokens. Kept free of view imports: DRF imports this
module while it is still loading its own views."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, TokenAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied
from rest_framework.permissions import SAFE_METHODS


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


# --- personal API tokens ----------------------------------------------------------

API_TOKEN_PREFIX = "op_"


def hash_key(key: str) -> str:
    import hashlib

    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key() -> tuple[str, str]:
    """(full key, lookup prefix). The key is shown once and never stored."""
    import secrets

    prefix = secrets.token_hex(5)
    return f"{API_TOKEN_PREFIX}{prefix}_{secrets.token_urlsafe(32)}", prefix


class PersonalTokenAuthentication(BaseAuthentication):
    """`Authorization: Bearer op_...`. Read-only tokens may only GET."""

    keyword = "Bearer"

    def authenticate(self, request):
        import hmac

        from .models import ApiToken

        auth = get_authorization_header(request).decode(errors="ignore").split()
        if len(auth) != 2 or auth[0] != self.keyword or not auth[1].startswith(API_TOKEN_PREFIX):
            return None
        key = auth[1]
        prefix = key[len(API_TOKEN_PREFIX) :].split("_", 1)[0]
        token = ApiToken.objects.select_related("user").filter(prefix=prefix).first()
        if token is None or not hmac.compare_digest(token.key_hash, hash_key(key)):
            raise AuthenticationFailed("Invalid API token.")
        now = timezone.now()
        if token.expires_at and token.expires_at <= now:
            raise AuthenticationFailed("This API token has expired.")
        if not token.user.is_active:
            raise AuthenticationFailed("This account is disabled.")
        if token.read_only and request.method not in SAFE_METHODS:
            raise PermissionDenied("This API token is read-only.")
        # Saving on every request would be a write per call; once a minute is plenty.
        if token.last_used_at is None or now - token.last_used_at > timedelta(minutes=1):
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)
        return token.user, token

    def authenticate_header(self, request):
        return self.keyword


def is_personal_token(request) -> bool:
    from .models import ApiToken

    return isinstance(getattr(request, "auth", None), ApiToken)


# --- sign-in cookie -----------------------------------------------------------------

# The web app signs in with an httpOnly cookie, so a script injected into the
# page can't read the session token. Cookies are sent by the browser on
# cross-site requests too, so every cookie-authenticated request must also
# carry this header: a browser only lets another site send a custom header
# after a CORS preflight, and CORS allows the app's own origin only.
COOKIE_HEADER = "X-OmniPost-Client"
COOKIE_HEADER_META = "HTTP_X_OMNIPOST_CLIENT"


class CookieTokenAuthentication(ExpiringTokenAuthentication):
    def authenticate(self, request):
        key = request.COOKIES.get(settings.AUTH_COOKIE_NAME)
        if not key or not request.META.get(COOKIE_HEADER_META):
            return None
        return self.authenticate_credentials(key)

    def authenticate_header(self, request):
        return "Token"
