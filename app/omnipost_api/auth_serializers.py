"""Password-reset emails that link to the frontend's /reset-password page
instead of a Django-rendered one."""

from __future__ import annotations

from dj_rest_auth.serializers import PasswordResetSerializer
from django.conf import settings


def frontend_reset_url(request, user, temp_key):
    """Reset emails point at the UI's /reset-password page, which POSTs
    uid + token to /auth/password/reset/confirm/."""
    from allauth.account.utils import user_pk_to_url_str

    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/reset-password?uid={user_pk_to_url_str(user)}&token={temp_key}"


class FrontendPasswordResetSerializer(PasswordResetSerializer):
    def get_email_options(self):
        return {"url_generator": frontend_reset_url}
