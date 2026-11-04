"""Password-reset emails that link to the frontend's /reset-password page
instead of a Django-rendered one."""

from __future__ import annotations

from dj_rest_auth.serializers import PasswordResetSerializer
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework import serializers


def frontend_reset_url(request, user, temp_key):
    """Reset emails point at the UI's /reset-password page, which POSTs
    uid + token to /auth/password/reset/confirm/."""
    from allauth.account.utils import user_pk_to_url_str

    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/reset-password?uid={user_pk_to_url_str(user)}&token={temp_key}"


class FrontendPasswordResetSerializer(PasswordResetSerializer):
    def get_email_options(self):
        return {"url_generator": frontend_reset_url}


class UserDetailsSerializer(serializers.ModelSerializer):
    """/auth/user/: dj-rest-auth's fields plus the email-alerts preference."""

    class Meta:
        model = get_user_model()
        fields = ["pk", "username", "email", "first_name", "last_name", "email_notifications", "weekly_digest"]
        read_only_fields = ["pk", "email"]
