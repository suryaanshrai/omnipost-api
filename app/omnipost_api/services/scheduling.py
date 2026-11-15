"""Putting posts on the schedule: one place for the rules the API, CSV
import, recurring posts and reposting all share."""

from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.utils import timezone

from ..models import Post, PostTarget, PostTargetPart, User


class NothingToSchedule(ValueError):
    pass


def _targets(post: Post) -> list[PostTarget]:
    targets = list(post.targets.exclude(status=PostTarget.STATUS_CANCELED))
    if not targets:
        raise NothingToSchedule("This post has no channel targets to schedule.")
    return targets


def schedule_post(post: Post, run_at: datetime | None = None) -> Post:
    """Every target at the same time; now if no time is given."""
    from jobs.scheduler import schedule_post_target

    from . import billing

    targets = _targets(post)
    billing.check_scheduled_limit(post)
    run_at = run_at or timezone.now()
    for target in targets:
        schedule_post_target(target, run_at=run_at)
    post.status = Post.STATUS_SCHEDULED
    post.scheduled_for = run_at
    post.save(update_fields=["status", "scheduled_for", "updated_at"])
    return post


def queue_post(post: Post) -> Post:
    """Each target in its channel's next open queue slot. Raises
    queueing.NoOpenSlotError if a channel has none."""
    from jobs.scheduler import schedule_post_target

    from . import billing
    from .queueing import next_open_slot_run_at

    targets = _targets(post)
    billing.check_scheduled_limit(post)
    resolved = []
    for target in targets:
        run_at = next_open_slot_run_at(target.channel)
        schedule_post_target(target, run_at=run_at)
        resolved.append(run_at)
    post.status = Post.STATUS_SCHEDULED
    post.scheduled_for = min(resolved)
    post.save(update_fields=["status", "scheduled_for", "updated_at"])
    return post


@transaction.atomic
def duplicate_post(source: Post, author: User | None) -> Post:
    """A new draft with the same text, media, link, channels, per-channel
    text and thread parts."""
    copy = Post.objects.create(
        workspace=source.workspace,
        author=author,
        kind=source.kind,
        base_text=source.base_text,
        link=source.link,
        voice_profile=source.voice_profile,
        status=Post.STATUS_DRAFT,
    )
    copy.base_media.set(source.base_media.all())
    for target in source.targets.all():
        new_target = PostTarget.objects.create(
            post=copy,
            channel=target.channel,
            format=target.format,
            text_override=target.text_override,
            first_comment=target.first_comment,
        )
        new_target.media.set(target.media.all())
        for part in target.parts.all():
            new_part = PostTargetPart.objects.create(
                post_target=new_target, sequence=part.sequence, text=part.text, delay_after_s=part.delay_after_s
            )
            new_part.media.set(part.media.all())
    return copy
