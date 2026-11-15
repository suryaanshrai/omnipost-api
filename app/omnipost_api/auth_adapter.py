"""allauth adapter: verification links point at the frontend, which POSTs
the key to /auth/registration/verify-email/."""

from __future__ import annotations

from allauth.account.adapter import DefaultAccountAdapter
from django.conf import settings


class FrontendAccountAdapter(DefaultAccountAdapter):
    def get_email_confirmation_url(self, request, emailconfirmation):
        return f"{settings.FRONTEND_URL.rstrip('/')}/verify-email?key={emailconfirmation.key}"
