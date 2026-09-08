import pytest
from ai import quota
from ai.errors import QuotaExceeded
from omnipost_api.models import UsageCounter


def test_reserve_up_to_limit_then_blocks(workspace, settings):
    settings.AI_FREE_TEXT_LIMIT = 2
    quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)
    quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)
    with pytest.raises(QuotaExceeded):
        quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)


def test_commit_moves_reserved_to_committed(workspace, settings):
    settings.AI_FREE_TEXT_LIMIT = 3
    reservation = quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)
    quota.commit(reservation)

    counter = UsageCounter.objects.get(workspace=workspace, feature=UsageCounter.FEATURE_AI_TEXT)
    assert counter.reserved == 0
    assert counter.committed == 1


def test_release_frees_reservation_without_committing(workspace, settings):
    settings.AI_FREE_TEXT_LIMIT = 1
    reservation = quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)
    quota.release(reservation)

    counter = UsageCounter.objects.get(workspace=workspace, feature=UsageCounter.FEATURE_AI_TEXT)
    assert counter.reserved == 0
    assert counter.committed == 0

    # Freed slot is usable again — this is what makes "failed calls don't
    # burn quota" actually true rather than just not-yet-counted.
    quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)


def test_reserved_and_committed_both_count_against_limit(workspace, settings):
    settings.AI_FREE_TEXT_LIMIT = 1
    quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)  # never committed or released
    with pytest.raises(QuotaExceeded):
        quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)


def test_unlimited_when_limit_is_none(workspace):
    counter = UsageCounter.objects.create(
        workspace=workspace, feature=UsageCounter.FEATURE_AI_TEXT, period="lifetime", limit=None
    )
    for _ in range(5):
        quota.commit(quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT))
    counter.refresh_from_db()
    assert counter.committed == 5
