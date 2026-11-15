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


def quota_workspace(workspace: Workspace) -> Workspace:
    """The free allowance belongs to the account that owns the workspace,
    kept on that owner's first workspace. Otherwise creating another
    workspace (free and unlimited) reset the allowance."""
    from omnipost_api.models import Membership

    owner = (
        Membership.objects.filter(workspace=workspace, role=Membership.ROLE_OWNER).order_by("created_at").first()
    )
    if owner is None:
        return workspace
    first = (
        Workspace.objects.filter(memberships__user_id=owner.user_id, memberships__role=Membership.ROLE_OWNER)
        .order_by("created_at", "pk")
        .first()
    )
    return first or workspace


def _period_and_limit(workspace: Workspace, feature: str) -> tuple[str, int | None]:
    """A paid hosted plan gets a monthly allowance; everyone else the small
    lifetime one."""
    from django.utils import timezone
    from omnipost_api.services import billing

    monthly = billing.ai_monthly_limit(workspace, feature)
    if monthly is not None:
        return timezone.now().strftime("%Y-%m"), monthly
    return _FREE_PERIOD, _default_limit(feature)


def reserve(workspace: Workspace, feature: str) -> Reservation:
    workspace = quota_workspace(workspace)
    period, limit = _period_and_limit(workspace, feature)
    with transaction.atomic():
        counter, _ = UsageCounter.objects.get_or_create(
            workspace=workspace,
            feature=feature,
            period=period,
            defaults={"limit": limit},
        )
        # A monthly counter follows the plan (it may have changed this month).
        # The lifetime one keeps whatever limit it has, so an admin can raise it.
        if period != _FREE_PERIOD and counter.limit != limit:
            UsageCounter.objects.filter(pk=counter.pk).update(limit=limit)
        counter = UsageCounter.objects.select_for_update().get(pk=counter.pk)
        if counter.limit is not None and (counter.reserved + counter.committed) >= counter.limit:
            raise QuotaExceeded(
                f"{'This month' if period != _FREE_PERIOD else 'The free'} limit of {counter.limit} "
                f"is used up for {feature}. Add your own provider key to keep generating"
                f"{'' if period != _FREE_PERIOD else ', or upgrade'}."
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
    workspace = quota_workspace(workspace)
    periods = {_period_and_limit(workspace, f)[0] for f in _DEFAULT_LIMITS}
    return list(UsageCounter.objects.filter(workspace=workspace, period__in=periods))
