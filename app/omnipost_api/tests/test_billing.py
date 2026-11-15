"""Hosted plans: limits only with Stripe configured, Checkout/Portal, the
signed webhook, and the pricing fake door."""

import hashlib
import hmac
import json
import time

import pytest
import responses
from rest_framework.test import APIClient

from omnipost_api.models import Channel, Post, PostTarget, PricingInterest, User

STRIPE = "https://api.stripe.com/v1"


@pytest.fixture
def stripe_on(settings):
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    settings.STRIPE_WEBHOOK_SECRET = "whsec_test"
    settings.STRIPE_PRICE_CREATOR = "price_creator"
    settings.STRIPE_PRICE_PRO = "price_pro"
    settings.FRONTEND_URL = "https://app.example.com"
    return settings


def _setup(django_user_model, name="bill"):
    user = django_user_model.objects.create_user(username=name, email=f"{name}@example.com", password="x")
    workspace = user.memberships.get().workspace
    client = APIClient()
    client.force_authenticate(user=user)
    return user, workspace, client


def _channels(workspace, n):
    return [
        Channel.objects.create(workspace=workspace, connector_slug="bluesky", display_name=f"c{i}") for i in range(n)
    ]


def _draft(workspace, channel):
    post = Post.objects.create(workspace=workspace, kind="text", base_text="x")
    PostTarget.objects.create(post=post, channel=channel)
    return post


def test_self_hosted_has_no_limits(db, django_user_model, enqueued):
    user, workspace, client = _setup(django_user_model)
    (channel,) = _channels(workspace, 1)
    _channels(workspace, 5)
    for _ in range(12):
        assert client.post(f"/posts/{_draft(workspace, channel).pk}/schedule/", {}).status_code == 200
    billing = client.get("/billing/").data
    assert billing["enabled"] is False and billing["plan"] == "free"


def test_free_plan_caps_scheduled_posts(db, django_user_model, enqueued, stripe_on):
    user, workspace, client = _setup(django_user_model)
    (channel,) = _channels(workspace, 1)
    posts = [_draft(workspace, channel) for _ in range(11)]
    for post in posts[:10]:
        assert client.post(f"/posts/{post.pk}/schedule/", {}).status_code == 200
    blocked = client.post(f"/posts/{posts[10].pk}/schedule/", {})
    assert blocked.status_code == 402 and blocked.data["detail"].code == "plan_limit"
    assert client.get("/billing/").data["usage"]["scheduled_posts"] == 10
    # Cancelling one frees its place.
    client.post(f"/posts/{posts[0].pk}/cancel/")
    assert client.post(f"/posts/{posts[10].pk}/schedule/", {}).status_code == 200
    posts.append(_draft(workspace, channel))
    assert client.post(f"/posts/{posts[11].pk}/schedule/", {}).status_code == 402

    User.objects.filter(pk=user.pk).update(plan="creator")
    assert client.post(f"/posts/{posts[11].pk}/schedule/", {}).status_code == 200


def test_free_plan_caps_channels(db, django_user_model, stripe_on):
    user, workspace, client = _setup(django_user_model)
    _channels(workspace, 3)
    body = {"workspace": workspace.pk, "connector_slug": "bluesky", "display_name": "four"}
    response = client.post("/channels/", body, format="json")
    assert response.status_code == 402


def test_limit_counts_every_workspace_you_own(db, django_user_model, stripe_on):
    user, workspace, client = _setup(django_user_model)
    _channels(workspace, 3)
    second = client.post("/workspaces/", {"name": "Side project"}, format="json").data["id"]
    body = {"workspace": second, "connector_slug": "bluesky", "display_name": "x"}
    assert client.post("/channels/", body, format="json").status_code == 402


@responses.activate
def test_checkout_creates_customer_once_and_returns_url(db, django_user_model, stripe_on):
    user, _, client = _setup(django_user_model)
    responses.add(responses.POST, f"{STRIPE}/customers", json={"id": "cus_1"})
    responses.add(responses.POST, f"{STRIPE}/checkout/sessions", json={"url": "https://checkout.stripe.com/c/1"})
    assert client.post("/billing/checkout/", {"plan": "creator"}).data["url"] == "https://checkout.stripe.com/c/1"
    sent = responses.calls[1].request.body
    assert "line_items%5B0%5D%5Bprice%5D=price_creator" in sent and "client_reference_id=" in sent
    client.post("/billing/checkout/", {"plan": "pro"})
    assert sum(c.request.url.endswith("/customers") for c in responses.calls) == 1
    user.refresh_from_db()
    assert user.stripe_customer_id == "cus_1"


def test_checkout_refuses_without_stripe_and_unknown_plans(db, django_user_model, settings):
    _, _, client = _setup(django_user_model)
    assert client.post("/billing/checkout/", {"plan": "creator"}).status_code == 400
    settings.STRIPE_SECRET_KEY = "sk_test_x"
    assert client.post("/billing/checkout/", {"plan": "free"}).status_code == 400


def _signed(payload: dict, secret="whsec_test", ts=None):
    body = json.dumps(payload).encode()
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, f"t={ts},v1={sig}"


@responses.activate
def test_webhook_syncs_plan_from_stripe(db, django_user_model, stripe_on):
    user, _, _ = _setup(django_user_model)
    user.stripe_customer_id = "cus_1"
    user.save()
    sub = {
        "id": "sub_1",
        "customer": "cus_1",
        "status": "active",
        "items": {"data": [{"price": {"id": "price_pro"}, "current_period_end": 1893456000}]},
    }
    responses.add(responses.GET, f"{STRIPE}/subscriptions/sub_1", json=sub)
    body, sig = _signed({"id": "evt_1", "type": "customer.subscription.updated", "data": {"object": {"id": "sub_1"}}})
    response = APIClient().post("/billing/webhook/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig)
    assert response.status_code == 200
    user.refresh_from_db()
    assert (user.plan, user.plan_status, user.stripe_subscription_id) == ("pro", "active", "sub_1")
    assert user.plan_renews_at.year == 2030

    # Cancelled: Stripe says so when we re-read it, whatever the event claims.
    responses.replace(responses.GET, f"{STRIPE}/subscriptions/sub_1", json=sub | {"status": "canceled"})
    body, sig = _signed({"id": "evt_2", "type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}})
    APIClient().post("/billing/webhook/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig)
    user.refresh_from_db()
    assert user.plan == "free" and user.plan_renews_at is None


def test_webhook_rejects_bad_or_stale_signatures(db, stripe_on):
    body, sig = _signed({"type": "customer.subscription.updated"}, secret="wrong")
    client = APIClient()
    assert (
        client.post("/billing/webhook/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig).status_code
        == 400
    )
    body, sig = _signed({"type": "x"}, ts=int(time.time()) - 3600)
    assert (
        client.post("/billing/webhook/", body, content_type="application/json", HTTP_STRIPE_SIGNATURE=sig).status_code
        == 400
    )
    assert client.post("/billing/webhook/", body, content_type="application/json").status_code == 400


def test_paid_plan_gets_a_monthly_ai_allowance(db, django_user_model, stripe_on, settings):
    from ai import quota
    from ai.errors import QuotaExceeded

    from omnipost_api.models import UsageCounter

    settings.AI_FREE_TEXT_LIMIT = 1
    user, workspace, _ = _setup(django_user_model)
    quota.commit(quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT))
    with pytest.raises(QuotaExceeded):
        quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)
    User.objects.filter(pk=user.pk).update(plan="creator")
    reservation = quota.reserve(workspace, UsageCounter.FEATURE_AI_TEXT)
    assert UsageCounter.objects.get(pk=reservation.counter_id).limit == 300


def test_pricing_and_interest_fake_door(db, settings):
    settings.CLOUD_PRICING_ENABLED = True
    client = APIClient()
    pricing = client.get("/public/pricing/").data
    assert pricing["show_pricing"] and not pricing["billing_enabled"]
    assert [p["id"] for p in pricing["plans"]] == ["free", "creator", "pro"]
    assert not any(p["purchasable"] for p in pricing["plans"])

    assert client.post("/public/interest/", {"email": "a@b.co", "plan": "creator"}).status_code == 201
    assert client.post("/public/interest/", {"email": "nope", "plan": "creator"}).status_code == 400
    assert PricingInterest.objects.get().plan == "creator"
