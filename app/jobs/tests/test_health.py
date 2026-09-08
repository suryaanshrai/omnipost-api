from datetime import timedelta

from connectors.base import ChannelCredentials
from django.utils import timezone
from omnipost_api.models import AuditEvent, Channel, PostTarget

from jobs.health import REFRESH_THRESHOLD_HOURS, preflight_check, refresh_channel, refresh_expiring_channels
from jobs.scheduler import PREFLIGHT_LEAD_TIME, schedule_post_target


def test_refresh_channel_updates_credentials_and_expiry(mastodon_channel, monkeypatch):
    from connectors.registry import get as get_connector

    connector = get_connector("mastodon")

    def fake_refresh(credentials):
        return ChannelCredentials(values={**credentials.values, "ACCESS_TOKEN": "new-token"})

    monkeypatch.setattr(connector, "refresh", fake_refresh)
    monkeypatch.setattr(connector, "token_expiry", lambda creds: timezone.now() + timedelta(days=60))

    assert refresh_channel(mastodon_channel) is True
    mastodon_channel.refresh_from_db()
    assert mastodon_channel.get_credentials()["ACCESS_TOKEN"] == "new-token"
    assert mastodon_channel.token_expires_at is not None


def test_refresh_channel_marks_broken_on_auth_expired(mastodon_channel, monkeypatch):
    from connectors.errors import AuthExpired
    from connectors.registry import get as get_connector

    connector = get_connector("mastodon")

    def fake_refresh(credentials):
        raise AuthExpired("nope")

    monkeypatch.setattr(connector, "refresh", fake_refresh)

    assert refresh_channel(mastodon_channel) is False
    mastodon_channel.refresh_from_db()
    assert mastodon_channel.health == Channel.HEALTH_BROKEN


def test_refresh_expiring_channels_only_touches_due_ones(mastodon_channel, monkeypatch):
    from connectors.registry import get as get_connector

    connector = get_connector("mastodon")
    monkeypatch.setattr(connector, "refresh", lambda creds: creds)
    monkeypatch.setattr(connector, "token_expiry", lambda creds: None)

    mastodon_channel.token_expires_at = timezone.now() + timedelta(hours=1)
    mastodon_channel.save()

    result = refresh_expiring_channels()
    assert result == {"refreshed": 1, "broken": 0}


def test_refresh_expiring_channels_ignores_far_future_expiry(mastodon_channel):
    mastodon_channel.token_expires_at = timezone.now() + timedelta(hours=REFRESH_THRESHOLD_HOURS + 10)
    mastodon_channel.save()

    result = refresh_expiring_channels()
    assert result == {"refreshed": 0, "broken": 0}


def test_preflight_check_fails_for_broken_channel(post_target):
    post_target.channel.health = Channel.HEALTH_BROKEN
    post_target.channel.save()
    assert preflight_check(post_target) is False


def test_preflight_check_passes_for_healthy_channel_with_no_expiry(post_target):
    assert preflight_check(post_target) is True


def test_scheduling_far_in_advance_enqueues_a_preflight_job(post_target, enqueued):
    run_at = timezone.now() + timedelta(hours=2)
    schedule_post_target(post_target, run_at=run_at)
    assert any(kind == "preflight" for kind, _ in enqueued)


def test_scheduling_immediately_skips_preflight(post_target, enqueued):
    schedule_post_target(post_target, run_at=timezone.now())
    assert not any(kind == "preflight" for kind, _ in enqueued)


def test_run_preflight_for_target_records_audit_event_on_failure(post_target):
    from jobs.health import run_preflight_for_target

    post_target.status = PostTarget.STATUS_SCHEDULED
    post_target.run_at = timezone.now() + PREFLIGHT_LEAD_TIME
    post_target.save()
    post_target.channel.health = Channel.HEALTH_BROKEN
    post_target.channel.save()

    run_preflight_for_target(post_target.pk)

    event = AuditEvent.objects.get(verb="channel.preflight_failed")
    assert event.target_id == str(post_target.channel_id)


def test_run_preflight_for_target_is_a_noop_if_already_published(post_target):
    from jobs.health import run_preflight_for_target

    post_target.status = PostTarget.STATUS_PUBLISHED
    post_target.save()
    post_target.channel.health = Channel.HEALTH_BROKEN
    post_target.channel.save()

    run_preflight_for_target(post_target.pk)

    assert not AuditEvent.objects.filter(verb="channel.preflight_failed").exists()
