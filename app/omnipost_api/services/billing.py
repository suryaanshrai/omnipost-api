"""Hosted plans: what each allows, enforcing it, and Stripe.

Everything here is off unless STRIPE_SECRET_KEY is set. A self-hosted
install has no plans and no limits.

Stripe is called over its REST API with `requests` (a handful of calls,
no SDK needed). Webhooks are checked against STRIPE_WEBHOOK_SECRET, and the
subscription is always re-read from Stripe rather than trusted from the
event, so late or out-of-order events can't leave the wrong plan behind.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests
from django.conf import settings
from rest_framework import status
from rest_framework.exceptions import APIException

from ..models import Channel, Membership, Post, User, Workspace

_API = "https://api.stripe.com/v1"
# Subscription states that keep the paid plan. past_due keeps it while
# Stripe retries the card; anything else drops back to free.
_PAID_STATES = {"active", "trialing", "past_due"}


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    channels: int | None  # None = unlimited
    scheduled_posts: int | None  # posts waiting to go out at once
    ai_text_per_month: int | None
    ai_image_per_month: int | None
    features: tuple[str, ...]

    @property
    def price_display(self) -> str:
        return {
            User.PLAN_CREATOR: settings.PRICE_DISPLAY_CREATOR,
            User.PLAN_PRO: settings.PRICE_DISPLAY_PRO,
        }.get(self.id, "$0")

    @property
    def stripe_price(self) -> str:
        return {User.PLAN_CREATOR: settings.STRIPE_PRICE_CREATOR, User.PLAN_PRO: settings.STRIPE_PRICE_PRO}.get(
            self.id, ""
        )


PLANS: dict[str, Plan] = {
    User.PLAN_FREE: Plan(
        User.PLAN_FREE,
        "Free",
        channels=3,
        scheduled_posts=10,
        ai_text_per_month=None,  # the small lifetime allowance in ai/quota.py
        ai_image_per_month=None,
        features=("3 channels", "10 scheduled posts at a time", "A few AI drafts to try it"),
    ),
    User.PLAN_CREATOR: Plan(
        User.PLAN_CREATOR,
        "Creator",
        channels=None,
        scheduled_posts=None,
        ai_text_per_month=300,
        ai_image_per_month=30,
        features=("Unlimited channels and scheduling", "300 AI drafts and 30 images a month", "Analytics"),
    ),
    User.PLAN_PRO: Plan(
        User.PLAN_PRO,
        "Pro",
        channels=None,
        scheduled_posts=None,
        ai_text_per_month=1500,
        ai_image_per_month=150,
        features=("Everything in Creator", "1,500 AI drafts and 150 images a month", "Priority support"),
    ),
}


def enabled() -> bool:
    return bool(settings.STRIPE_SECRET_KEY)


class PlanLimitReached(APIException):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_code = "plan_limit"


def owner_of(workspace: Workspace | int) -> User | None:
    workspace_id = workspace if isinstance(workspace, int) else workspace.pk
    membership = (
        Membership.objects.filter(workspace_id=workspace_id, role=Membership.ROLE_OWNER)
        .select_related("user")
        .order_by("created_at")
        .first()
    )
    return membership.user if membership else None


def plan_for(user: User | None) -> Plan:
    return PLANS.get(user.plan if user else User.PLAN_FREE, PLANS[User.PLAN_FREE])


def _owned_workspaces(user: User):
    return Workspace.objects.filter(memberships__user=user, memberships__role=Membership.ROLE_OWNER)


def usage(user: User) -> dict[str, int]:
    owned = _owned_workspaces(user)
    return {
        "channels": Channel.objects.filter(workspace__in=owned).count(),
        "scheduled_posts": Post.objects.filter(workspace__in=owned, status=Post.STATUS_SCHEDULED).count(),
    }


def check_channel_limit(workspace: Workspace | int) -> None:
    """Before connecting a new channel (reconnecting one doesn't count)."""
    if not enabled():
        return
    owner = owner_of(workspace)
    plan = plan_for(owner)
    if owner is None or plan.channels is None:
        return
    if usage(owner)["channels"] >= plan.channels:
        raise PlanLimitReached(
            f"The {plan.name} plan includes {plan.channels} channels. Upgrade to connect more, "
            "or remove one you don't use."
        )


def check_scheduled_limit(post: Post) -> None:
    """Before putting a post on the schedule. Rescheduling one that's
    already scheduled doesn't count twice."""
    if not enabled() or post.status == Post.STATUS_SCHEDULED:
        return
    owner = owner_of(post.workspace_id)
    plan = plan_for(owner)
    if owner is None or plan.scheduled_posts is None:
        return
    if usage(owner)["scheduled_posts"] >= plan.scheduled_posts:
        raise PlanLimitReached(
            f"The {plan.name} plan can hold {plan.scheduled_posts} scheduled posts at a time. "
            "Upgrade, or wait for some to go out."
        )


def ai_monthly_limit(workspace: Workspace, feature: str) -> int | None:
    """A paid plan's monthly AI allowance, or None to use the free one."""
    from omnipost_api.models import UsageCounter

    if not enabled():
        return None
    plan = plan_for(owner_of(workspace))
    return {
        UsageCounter.FEATURE_AI_TEXT: plan.ai_text_per_month,
        UsageCounter.FEATURE_AI_IMAGE: plan.ai_image_per_month,
    }.get(feature)


# --- Stripe ----------------------------------------------------------------------------


class StripeError(Exception):
    pass


def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: {"a": {"b": 1}, "l": [x]} -> a[b]=1, l[0]=x."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            out += _flatten(value, name)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                out += _flatten(item, f"{name}[{i}]") if isinstance(item, dict) else [(f"{name}[{i}]", str(item))]
        elif value is not None:
            out.append((name, str(value).lower() if isinstance(value, bool) else str(value)))
    return out


def _stripe(method: str, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    response = requests.request(
        method,
        f"{_API}{path}",
        auth=(settings.STRIPE_SECRET_KEY, ""),
        data=_flatten(data or {}),
        timeout=20,
    )
    body = response.json() if response.content else {}
    if response.status_code >= 400:
        raise StripeError((body.get("error") or {}).get("message") or f"Stripe returned {response.status_code}")
    return body


def _customer(user: User) -> str:
    if not user.stripe_customer_id:
        customer = _stripe("POST", "/customers", {"email": user.email, "metadata": {"omnipost_user": user.pk}})
        user.stripe_customer_id = customer["id"]
        user.save(update_fields=["stripe_customer_id"])
    return user.stripe_customer_id


def checkout_url(user: User, plan_id: str) -> str:
    plan = PLANS.get(plan_id)
    if plan is None or not plan.stripe_price:
        raise StripeError("That plan isn't for sale here.")
    base = settings.FRONTEND_URL.rstrip("/")
    session = _stripe(
        "POST",
        "/checkout/sessions",
        {
            "mode": "subscription",
            "customer": _customer(user),
            "client_reference_id": user.pk,
            "line_items": [{"price": plan.stripe_price, "quantity": 1}],
            "success_url": f"{base}/settings?billing=success",
            "cancel_url": f"{base}/settings?billing=canceled",
            "allow_promotion_codes": True,
        },
    )
    return session["url"]


def portal_url(user: User) -> str:
    if not user.stripe_customer_id:
        raise StripeError("There's no billing account yet. Choose a plan first.")
    session = _stripe(
        "POST",
        "/billing_portal/sessions",
        {"customer": user.stripe_customer_id, "return_url": f"{settings.FRONTEND_URL.rstrip('/')}/settings"},
    )
    return session["url"]


def verify_webhook(payload: bytes, signature_header: str, tolerance_s: int = 300) -> None:
    """Checks the Stripe-Signature header (t=...,v1=...). Raises StripeError."""
    secret = settings.STRIPE_WEBHOOK_SECRET
    if not secret:
        raise StripeError("STRIPE_WEBHOOK_SECRET isn't set.")
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts.get("t", [""])[0])
    except ValueError:
        raise StripeError("Bad signature header.") from None
    if abs(time.time() - timestamp) > tolerance_s:
        raise StripeError("Signature is too old.")
    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in parts.get("v1", [])):
        raise StripeError("Signature doesn't match.")


def _plan_for_price(price_id: str) -> str:
    for plan in PLANS.values():
        if plan.stripe_price and plan.stripe_price == price_id:
            return plan.id
    return User.PLAN_FREE


def sync_subscription(subscription_id: str) -> User | None:
    """Reads the subscription from Stripe and sets its customer's plan."""
    sub = _stripe("GET", f"/subscriptions/{subscription_id}")
    user = User.objects.filter(stripe_customer_id=sub.get("customer") or "").first()
    if user is None:
        return None
    items = (sub.get("items") or {}).get("data") or []
    price_id = (items[0].get("price") or {}).get("id", "") if items else ""
    paid = sub.get("status") in _PAID_STATES
    period_end = sub.get("current_period_end") or (items[0].get("current_period_end") if items else None)
    user.plan = _plan_for_price(price_id) if paid else User.PLAN_FREE
    user.plan_status = sub.get("status") or ""
    user.stripe_subscription_id = sub["id"] if paid else ""
    user.plan_renews_at = datetime.fromtimestamp(period_end, tz=UTC) if period_end and paid else None
    user.save(update_fields=["plan", "plan_status", "stripe_subscription_id", "plan_renews_at"])
    return user


def handle_event(event: dict[str, Any]) -> str:
    """Applies one verified webhook event. Returns what happened, for logs."""
    kind = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if kind == "checkout.session.completed":
        user_id = obj.get("client_reference_id")
        if user_id and obj.get("customer"):
            User.objects.filter(pk=user_id, stripe_customer_id="").update(stripe_customer_id=obj["customer"])
        if obj.get("subscription"):
            sync_subscription(obj["subscription"])
            return "synced"
    elif kind.startswith("customer.subscription."):
        sync_subscription(obj["id"])
        return "synced"
    return "ignored"
