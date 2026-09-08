"""Resolves a QueueSlot into a concrete run_at: "add to queue" fills the next
open slot for a channel, in that channel's own local time, skipping slots
that are already occupied, inside a BlackoutWindow, or too close to another
scheduled post on the same channel (Channel.min_gap_minutes). See
jobs/scheduler.py for what happens once a run_at is resolved — this module
only answers "when", not "how it gets enqueued".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db.models import Q
from django.utils import timezone as dj_timezone

from ..models import BlackoutWindow, Channel, PostTarget

# How many days forward to search before giving up — a channel with no
# active QueueSlot, or slots that are perpetually blacked out, must fail
# loudly rather than hang.
_SEARCH_HORIZON_DAYS = 60

_NON_OCCUPYING_STATUSES = (PostTarget.STATUS_CANCELED, PostTarget.STATUS_FAILED)


class NoOpenSlotError(Exception):
    """No queue slot resolved to a usable run_at within the search horizon."""


def next_open_slot_run_at(channel: Channel, after: datetime | None = None) -> datetime:
    after = after or dj_timezone.now()
    tz_name = channel.effective_timezone()
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:
        raise NoOpenSlotError(f"Unknown timezone '{tz_name}' for {channel}.") from exc

    slots = list(channel.queue_slots.filter(is_active=True))
    if not slots:
        raise NoOpenSlotError(f"{channel} has no active queue slots configured.")

    min_gap = timedelta(minutes=channel.min_gap_minutes)
    # run_at__isnull=False already guarantees this at the DB level; the `if`
    # just narrows the type for mypy, since DateTimeField(null=True) means
    # the column's Python type is `datetime | None` regardless of the filter.
    occupied: list[datetime] = [
        t
        for t in PostTarget.objects.filter(channel=channel, run_at__isnull=False, run_at__gte=after - min_gap)
        .exclude(status__in=_NON_OCCUPYING_STATUSES)
        .values_list("run_at", flat=True)
        if t is not None
    ]
    blackout_windows = list(
        BlackoutWindow.objects.filter(workspace=channel.workspace_id, is_active=True).filter(
            Q(channel=channel) | Q(channel__isnull=True)
        )
    )

    candidate_date = after.astimezone(tz).date()
    for _ in range(_SEARCH_HORIZON_DAYS):
        weekday = candidate_date.weekday()
        for slot in slots:
            if slot.weekday != weekday:
                continue
            local_dt = datetime.combine(candidate_date, slot.time_of_day, tzinfo=tz)
            candidate = local_dt.astimezone(UTC)
            if candidate <= after:
                continue
            if any(candidate == t or abs((candidate - t).total_seconds()) < min_gap.total_seconds() for t in occupied):
                continue
            if _in_blackout(candidate, blackout_windows):
                continue
            return candidate
        candidate_date += timedelta(days=1)

    raise NoOpenSlotError(f"No open queue slot for {channel} in the next {_SEARCH_HORIZON_DAYS} days.")


def _in_blackout(candidate: datetime, windows: list[BlackoutWindow]) -> bool:
    return any(w.starts_at <= candidate <= w.ends_at for w in windows)
