"""SSRF guards, rate limits, token expiry, password reset and log redaction."""

import logging
from datetime import timedelta

import pytest
from connectors.netguard import UnsafeURL, assert_public_domain, assert_public_url, safe_get
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from app.logging_filters import RedactSecretsFilter


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# --- SSRF -----------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:6379/",
        "http://10.0.0.5/",
        "http://localhost/admin",
        "http://[::1]/",
        "ftp://example.com/file",
        "http://user:pw@example.com/",
    ],
)
def test_private_or_odd_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        assert_public_url(url)


def test_public_ip_url_is_allowed():
    assert_public_url("https://93.184.216.34/post")


@pytest.mark.parametrize("domain", ["127.0.0.1", "localhost", "mastodon.social/evil", "a@b.com", "x.internal"])
def test_bad_instance_domains_are_refused(domain):
    with pytest.raises(UnsafeURL):
        assert_public_domain(domain)


def test_safe_get_refuses_a_redirect_into_the_private_network():
    import responses

    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, "https://93.184.216.34/a", status=302, headers={"Location": "http://169.254.169.254/"})
        with pytest.raises(UnsafeURL):
            safe_get("https://93.184.216.34/a", timeout=5)


def test_discord_channel_must_use_a_discord_webhook(db, django_user_model):
    user = django_user_model.objects.create_user(username="d1", password="x")
    workspace = user.memberships.get().workspace
    response = _client_for(user).post(
        "/channels/",
        {
            "workspace": workspace.pk,
            "connector_slug": "discord",
            "display_name": "D",
            "credentials": {"WEBHOOK_URL": "http://10.0.0.1/hook"},
        },
        format="json",
    )
    assert response.status_code == 400


def test_unknown_connector_slug_is_rejected(db, django_user_model):
    user = django_user_model.objects.create_user(username="d2", password="x")
    workspace = user.memberships.get().workspace
    response = _client_for(user).post(
        "/channels/", {"workspace": workspace.pk, "connector_slug": "myspace", "display_name": "M"}, format="json"
    )
    assert response.status_code == 400


def test_repurpose_refuses_internal_source_url(db, django_user_model):
    user = django_user_model.objects.create_user(username="d3", password="x")
    workspace = user.memberships.get().workspace
    response = _client_for(user).post(
        "/ai/repurpose/",
        {"workspace": workspace.pk, "source_url": "http://169.254.169.254/latest/", "target_formats": ["thread"]},
        format="json",
    )
    assert response.status_code == 400


# --- rate limits ------------------------------------------------------------


def test_login_is_rate_limited(db, django_user_model):
    # DRF reads throttle rates at import time, so this uses the real "auth"
    # rate (THROTTLE_AUTH, default 10/min).
    django_user_model.objects.create_user(username="rl", password="x")
    client = APIClient()
    codes = [
        client.post("/auth/login/", {"username": "rl", "password": "wrong"}, format="json").status_code
        for _ in range(12)
    ]
    assert codes[0] == 400
    assert codes[-1] == 429


# --- token expiry -------------------------------------------------------------


def test_expired_token_is_rejected_and_a_new_login_issues_a_fresh_one(db, django_user_model):
    user = django_user_model.objects.create_user(username="exp", password="pw-123-long-enough")
    old = Token.objects.create(user=user)
    Token.objects.filter(pk=old.pk).update(created=timezone.now() - timedelta(days=400))

    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {old.key}")
    assert client.get("/workspaces/").status_code == 401
    assert not Token.objects.filter(pk=old.pk).exists()

    login = APIClient().post("/auth/login/", {"username": "exp", "password": "pw-123-long-enough"}, format="json")
    assert login.status_code == 200
    assert login.data["key"] != old.key


# --- password reset -------------------------------------------------------------


@override_settings(
    FRONTEND_URL="https://app.example.com", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"
)
def test_password_reset_email_links_to_the_frontend(db, django_user_model):
    django_user_model.objects.create_user(username="pr", email="pr@example.com", password="x")
    response = APIClient().post("/auth/password/reset/", {"email": "pr@example.com"}, format="json")
    assert response.status_code == 200
    assert len(mail.outbox) == 1
    assert "https://app.example.com/reset-password?uid=" in mail.outbox[0].body


# --- log redaction -------------------------------------------------------------


def test_tracebacks_are_redacted():
    try:
        raise RuntimeError("POST https://api.telegram.org/bot123456:ABCdef_secret/sendMessage failed")
    except RuntimeError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", None, exc_info=__import__("sys").exc_info())
    RedactSecretsFilter().filter(record)
    assert "ABCdef_secret" not in record.exc_text
    assert "[REDACTED]" in record.exc_text
