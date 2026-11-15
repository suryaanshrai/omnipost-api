"""Monday email: what each of your workspaces published (and what failed)
last week. Opt out with User.weekly_digest."""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone
from omnipost_api.models import Membership, PostTarget, User


def send_due_digests(now=None) -> dict[str, int]:
    now = now or timezone.now()
    if now.weekday() != 0:  # Mondays only
        return {"sent": 0}
    week_ago = now - timedelta(days=7)
    sent = 0
    users = User.objects.filter(weekly_digest=True, is_active=True).exclude(email="")
    for user in users:
        if user.last_digest_at and user.last_digest_at > now - timedelta(days=6):
            continue
        lines = []
        for membership in Membership.objects.filter(user=user).select_related("workspace"):
            targets = PostTarget.objects.filter(post__workspace=membership.workspace, updated_at__gte=week_ago)
            published = targets.filter(status=PostTarget.STATUS_PUBLISHED).count()
            failed = targets.filter(status=PostTarget.STATUS_FAILED).count()
            if published or failed:
                line = f"{membership.workspace.name}: {published} delivered"
                lines.append(line + (f", {failed} failed" if failed else ""))
        if not lines:
            continue
        body = (
            "Here's last week on OmniPost.\n\n"
            + "\n".join(lines)
            + f"\n\nOpen OmniPost: {settings.FRONTEND_URL}/app\n\nTurn this email off in Settings."
        )
        send_mail("[OmniPost] Your week", body, None, [user.email])
        user.last_digest_at = now
        user.save(update_fields=["last_digest_at"])
        sent += 1
    return {"sent": sent}
