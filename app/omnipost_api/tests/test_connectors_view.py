"""GET /connectors/ is the new single source of truth for connector
capabilities and gating (slug, display name, char limits, post kinds,
requires_own_app, media rules) — previously nowhere over HTTP, which is
exactly why the old frontend's hand-typed 3-platform table silently drifted
from the real (eleven-connector) registry. These tests pin its shape against
connectors.registry directly, so the two can't drift apart again unnoticed."""

from connectors import registry
from rest_framework.test import APIClient


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_connectors_requires_auth(db):
    response = APIClient().get("/connectors/")
    assert response.status_code == 401


def test_connectors_lists_every_registered_slug(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")

    response = _client_for(user).get("/connectors/")

    assert response.status_code == 200
    slugs = {row["slug"] for row in response.data}
    assert slugs == set(registry.all_slugs())
    assert len(response.data) == len(registry.all_slugs())


def test_connectors_reports_requires_own_app_for_gated_platforms(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")

    response = _client_for(user).get("/connectors/")

    by_slug = {row["slug"]: row for row in response.data}
    assert by_slug["linkedin"]["requires_own_app"] is True
    assert by_slug["x"]["requires_own_app"] is True
    assert by_slug["mastodon"]["requires_own_app"] is False
    assert by_slug["bluesky"]["requires_own_app"] is False


def test_connectors_report_how_each_platform_connects(db, django_user_model):
    """The Connect modal is driven entirely by these three fields: OAuth
    redirect, pasted credentials, or OAuth after collecting extra input."""
    user = django_user_model.objects.create_user(username="dana", password="x")

    response = _client_for(user).get("/connectors/")

    by_slug = {row["slug"]: row for row in response.data}
    # OAuth-only
    assert by_slug["instagram"]["supports_oauth"] is True
    assert by_slug["instagram"]["credential_fields"] == []
    # Manual-only
    assert by_slug["bluesky"]["supports_oauth"] is False
    assert by_slug["bluesky"]["credential_fields"] == ["IDENTIFIER", "APP_PASSWORD"]
    assert by_slug["discord"]["credential_fields"] == ["WEBHOOK_URL"]
    assert by_slug["telegram"]["credential_fields"] == ["BOT_TOKEN", "CHAT_ID"]
    # Both, with an extra OAuth input
    assert by_slug["mastodon"]["supports_oauth"] is True
    assert by_slug["mastodon"]["credential_fields"] == ["ACCESS_TOKEN", "INSTANCE_DOMAIN"]
    assert by_slug["mastodon"]["oauth_extra_fields"] == ["instance_domain"]
    # Every connector is connectable some way
    for row in response.data:
        assert row["supports_oauth"] or row["credential_fields"], row["slug"]


def test_connectors_capability_fields_match_the_registry(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    instagram = registry.get("instagram")

    response = _client_for(user).get("/connectors/")

    row = next(r for r in response.data if r["slug"] == "instagram")
    assert row["display_name"] == instagram.capabilities.display_name
    assert row["max_text_length"] == instagram.capabilities.max_text_length
    assert set(row["post_kinds"]) == set(instagram.capabilities.post_kinds)
    assert row["supports_alt_text"] == instagram.capabilities.supports_alt_text
    assert len(row["media"]) == len(instagram.capabilities.media)
    first_rule = row["media"][0]
    assert set(first_rule.keys()) == {
        "kinds",
        "max_count",
        "max_size_mb",
        "max_duration_s",
        "min_duration_s",
        "allowed_aspect_ratios",
        "allowed_mime_types",
    }
