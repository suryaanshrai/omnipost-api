"""Evergreen/recurring post recycling: rotates through a rule's text variants,
creating and scheduling a new Post+PostTarget each time next_run_at comes due.
Runs continuously via the `recur_loop` management command, mirroring
jobs/reconciler.py's Postgres-is-truth pattern — RecurrenceRule.next_run_at is
what's durable; nothing about "when is the next occurrence" lives in Redis.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone
from omnipost_api.models import Post, PostTarget, RecurrenceRule

from . import scheduler

logger = logging.getLogger(__name__)


def run_due_recurrences() -> int:
    now = timezone.now()
    due_ids = list(
        RecurrenceRule.objects.filter(is_active=True, next_run_at__lte=now).values_list("pk", flat=True)
    )
    count = 0
    for rule_id in due_ids:
        if _fire_once(rule_id, now):
            count += 1
    return count


def _fire_once(rule_id: int, now) -> bool:
    """Row-locks the rule so two racing workers can't both fire the same
    occurrence, then does the scheduling call outside the lock (it does its
    own I/O and shouldn't hold a row lock open while it does)."""
    with transaction.atomic():
        rule = RecurrenceRule.objects.select_for_update().get(pk=rule_id)
        if not rule.is_active or rule.next_run_at > now:
            return False  # lost a race, or already handled
        if rule.end_at and rule.end_at <= now:
            rule.is_active = False
            rule.save(update_fields=["is_active"])
            return False

        variants = rule.variants or [""]
        text = variants[rule.next_variant_index % len(variants)]

        post = Post.objects.create(
            workspace=rule.workspace, kind=rule.kind, base_text=text, link=rule.link, status=Post.STATUS_DRAFT
        )
        if rule.media.exists():
            post.base_media.set(rule.media.all())
        target = PostTarget.objects.create(post=post, channel=rule.channel)

        rule.next_variant_index += 1
        rule.next_run_at = rule.next_run_at + timedelta(hours=rule.interval_hours)
        rule.save(update_fields=["next_variant_index", "next_run_at"])

    scheduler.schedule_post_target(target, run_at=now)
    post.status = Post.STATUS_SCHEDULED
    post.scheduled_for = now
    post.save(update_fields=["status", "scheduled_for", "updated_at"])
    logger.info("Recurrence rule %s fired -> post %s", rule_id, post.pk)
    return True
