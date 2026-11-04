"""Sends the email for one Notification (services/notify.py)."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from omnipost_api.models import Notification

logger = logging.getLogger(__name__)


def send_notification_email(notification_id: int) -> None:
    notification = Notification.objects.select_related("user").filter(pk=notification_id).first()
    if notification is None or notification.emailed_at is not None:
        return
    user = notification.user
    if not user.email or not user.email_notifications:
        return
    link = f"{settings.FRONTEND_URL.rstrip('/')}{notification.link}" if notification.link else settings.FRONTEND_URL
    body = (
        f"{notification.body}\n\nOpen OmniPost: {link}\n\n"
        "You can turn these emails off in Settings."
    )
    send_mail(f"[OmniPost] {notification.title}", body, None, [user.email])
    notification.emailed_at = timezone.now()
    notification.save(update_fields=["emailed_at"])
