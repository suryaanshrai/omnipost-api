"""AI usage quota: reserve -> commit -> release.

A naive "check then increment" quota check races under concurrent requests
and, worse, charges a user's free-tier allotment for a call that then fails
partway through the provider request. Here the count is reserved *before*
the provider call and either committed (success) or released (failure) —
so a failed provider call never burns quota, and two concurrent requests
can't both slip through a limit of 1 by reading the same pre-increment
value. `reserved` and `committed` both count against `limit` so an
in-flight-but-unreserved race is impossible.

Only the managed provider path calls into this at all — BYOK usage costs the
workspace's own API bill, not OmniPost's, so it isn't metered here. See
gateway.run_text/run_image.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from omnipost_api.models import UsageCounter, Workspace

from .errors import QuotaExceeded

_FREE_PERIOD = "lifetime"

_DEFAULT_LIMITS = {
    UsageCounter.FEATURE_AI_TEXT: 3,
    UsageCounter.FEATURE_AI_IMAGE: 1,
}


@dataclass(frozen=True)
class Reservation:
    counter_id: int
    feature: str


def _default_limit(feature: str) -> int:
    from django.conf import settings

    overrides = {
        UsageCounter.FEATURE_AI_TEXT: settings.AI_FREE_TEXT_LIMIT,
        UsageCounter.FEATURE_AI_IMAGE: settings.AI_FREE_IMAGE_LIMIT,
    }
    return overrides.get(feature, _DEFAULT_LIMITS.get(feature, 0))


def reserve(workspace: Workspace, feature: str) -> Reservation:
    with transaction.atomic():
        counter, _ = UsageCounter.objects.get_or_create(
            workspace=workspace,
            feature=feature,
            period=_FREE_PERIOD,
            defaults={"limit": _default_limit(feature)},
        )
        counter = UsageCounter.objects.select_for_update().get(pk=counter.pk)
        if counter.limit is not None and (counter.reserved + counter.committed) >= counter.limit:
            raise QuotaExceeded(
                f"Free-tier limit of {counter.limit} reached for {feature}. "
                "Add a BYOK provider key to keep generating with your own quota."
            )
        counter.reserved += 1
        counter.save(update_fields=["reserved"])
    return Reservation(counter_id=counter.pk, feature=feature)


def commit(reservation: Reservation) -> None:
    with transaction.atomic():
        counter = UsageCounter.objects.select_for_update().get(pk=reservation.counter_id)
        counter.reserved = max(0, counter.reserved - 1)
        counter.committed += 1
        counter.save(update_fields=["reserved", "committed"])


def release(reservation: Reservation) -> None:
    with transaction.atomic():
        counter = UsageCounter.objects.select_for_update().get(pk=reservation.counter_id)
        counter.reserved = max(0, counter.reserved - 1)
        counter.save(update_fields=["reserved"])


def usage_for(workspace: Workspace) -> list[UsageCounter]:
    return list(UsageCounter.objects.filter(workspace=workspace, period=_FREE_PERIOD))
