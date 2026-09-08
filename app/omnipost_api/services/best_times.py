"""Best-time-to-post suggestions, derived only from a channel's own publish
history — never a fabricated global chart (see the revamp plan's positioning
notes on this being a real differentiator, not a generic "9am is best"
graphic). Buckets published PostTargets by (weekday, hour) in the channel's
effective timezone, scores each bucket by its average engagement (from each
target's latest PostMetric snapshot), and returns the top-scoring buckets
that meet a minimum sample size.

Needs metrics data to say anything at all — a freshly connected channel, or
one on a platform without a fetch_metrics() implementation yet (connectors/
base.py's default returns {}), simply gets no suggestions rather than a
misleading guess.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from omnipost_api.models import Channel, PostTarget

# A single outlier post shouldn't drive a suggestion.
MIN_SAMPLES_PER_BUCKET = 2
# Below this many total (bucket-eligible) data points, there's not enough
# history to say anything meaningful.
MIN_TOTAL_SAMPLES = 5


@dataclass(frozen=True)
class TimeSlotSuggestion:
    weekday: int  # 0 = Monday .. 6 = Sunday, matching QueueSlot's convention
    hour: int
    sample_size: int
    avg_engagement: float


def best_times_for_channel(channel: Channel, top_n: int = 5) -> list[TimeSlotSuggestion]:
    tz = ZoneInfo(channel.effective_timezone())
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)

    targets = PostTarget.objects.filter(
        channel=channel, status=PostTarget.STATUS_PUBLISHED, published_at__isnull=False
    ).prefetch_related("metrics")

    for target in targets:
        latest = target.metrics.first()  # PostMetric.Meta.ordering = ["-fetched_at"]
        if latest is None or target.published_at is None:
            continue
        local = target.published_at.astimezone(tz)
        buckets[(local.weekday(), local.hour)].append(latest.engagement_total())

    if sum(len(scores) for scores in buckets.values()) < MIN_TOTAL_SAMPLES:
        return []

    suggestions = [
        TimeSlotSuggestion(
            weekday=weekday, hour=hour, sample_size=len(scores), avg_engagement=sum(scores) / len(scores)
        )
        for (weekday, hour), scores in buckets.items()
        if len(scores) >= MIN_SAMPLES_PER_BUCKET
    ]
    suggestions.sort(key=lambda s: s.avg_engagement, reverse=True)
    return suggestions[:top_n]
