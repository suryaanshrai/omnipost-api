"""Connection Health: proactive token refresh and pre-flight checks.

Token expiry is the most common way these integrations silently break in
production — Meta long-lived tokens expire every 60 days, TikTok access
tokens every 24 hours, LinkedIn access tokens every 60 days with a hard
365-day refresh-token wall. The old system had no refresh logic at all: a
token just stopped working with no warning until a publish failed.

Two independent mechanisms:
  * refresh_expiring_channels() — a periodic sweep (maintenance queue / cron)
    that refreshes anything within its threshold of expiring.
  * preflight_check() — called shortly before a scheduled post's run_at, so a
    broken connection surfaces as a "reconnect this channel" notice before
    the scheduled time, not as a failed post after it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from connectors.errors import AuthExpired
from connectors.registry import get as get_connector
from django.utils import timezone
from omnipost_api.models import Channel, PostTarget

logger = logging.getLogger(__name__)

REFRESH_THRESHOLD_HOURS = 24


def refresh_expiring_channels() -> dict[str, int]:
    cutoff = timezone.now() + timedelta(hours=REFRESH_THRESHOLD_HOURS)
    due = Channel.objects.filter(
        token_expires_at__isnull=False, token_expires_at__lte=cutoff, health=Channel.HEALTH_HEALTHY
    )
    refreshed = 0
    broken = 0
    for channel in due:
        if refresh_channel(channel):
            refreshed += 1
        else:
            broken += 1
    return {"refreshed": refreshed, "broken": broken}


def refresh_channel(channel: Channel) -> bool:
    """Returns True on success. On AuthExpired, marks the channel broken so
    the user sees a reconnect prompt instead of a mysteriously failing post."""
    from connectors.base import ChannelCredentials

    connector = get_connector(channel.connector_slug)
    credentials = ChannelCredentials(values=channel.get_credentials(), external_account_id=channel.external_account_id)
    try:
        refreshed = connector.refresh(credentials)
    except AuthExpired:
        channel.health = Channel.HEALTH_BROKEN
        channel.health_detail = "Token refresh failed — reconnect this channel."
        channel.save(update_fields=["health", "health_detail", "updated_at"])
        logger.warning("Channel %s (%s) failed refresh, marked broken", channel.pk, channel.connector_slug)
        return False

    channel.set_credentials(refreshed.values)
    new_expiry = connector.token_expiry(refreshed)
    if new_expiry:
        channel.token_expires_at = new_expiry
    channel.save(update_fields=["token_expires_at", "updated_at"])
    return True


def preflight_check(post_target: PostTarget) -> bool:
    """Runs shortly before a scheduled publish. Returns True if the channel
    looks usable; refreshes proactively if it's near expiry. Does not itself
    reschedule or cancel anything — the caller (a maintenance-queue job)
    decides what to do with a False result (e.g. notify the user)."""
    channel = post_target.channel
    if channel.health == Channel.HEALTH_BROKEN:
        return False
    if channel.token_expires_at and channel.token_expires_at <= timezone.now() + timedelta(
        hours=REFRESH_THRESHOLD_HOURS
    ):
        return refresh_channel(channel)
    return True


def run_preflight_for_target(post_target_id: int) -> None:
    """RQ job entry point, enqueued ~30 minutes before a target's run_at by
    jobs.scheduler.schedule_post_target. Records an AuditEvent on failure so
    the user learns about a broken connection before the post fails, not
    after — this is what the "Connection Health" surface reads from."""
    from omnipost_api.models import AuditEvent

    try:
        post_target = PostTarget.objects.select_related("channel", "post").get(pk=post_target_id)
    except PostTarget.DoesNotExist:
        return

    if post_target.status not in (PostTarget.STATUS_SCHEDULED, PostTarget.STATUS_PENDING):
        return  # already published/canceled/failed by the time this ran

    if not preflight_check(post_target):
        AuditEvent.objects.create(
            workspace_id=post_target.post.workspace_id,
            verb="channel.preflight_failed",
            target_type="channel",
            target_id=str(post_target.channel_id),
            metadata={
                "post_target_id": post_target.pk,
                "channel_display_name": post_target.channel.display_name,
                "run_at": post_target.run_at.isoformat() if post_target.run_at else None,
            },
        )
        logger.warning(
            "Preflight failed for post_target %s on channel %s", post_target_id, post_target.channel_id
        )
