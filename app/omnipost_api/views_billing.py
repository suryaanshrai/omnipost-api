"""Hosted plans: your plan and usage, Stripe Checkout and Customer Portal,
Stripe's webhook, and the public price list / "tell me when" form."""

from __future__ import annotations

import json
import logging

from django.conf import settings
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth import NoPersonalToken
from .models import PricingInterest, User
from .services import billing

logger = logging.getLogger(__name__)


class PlanSerializer(serializers.Serializer):
    id = serializers.CharField()
    name = serializers.CharField()
    price_display = serializers.CharField()
    channels = serializers.IntegerField(allow_null=True)
    scheduled_posts = serializers.IntegerField(allow_null=True)
    features = serializers.ListField(child=serializers.CharField())
    purchasable = serializers.BooleanField()


def _plans() -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "price_display": p.price_display,
            "channels": p.channels,
            "scheduled_posts": p.scheduled_posts,
            "features": list(p.features),
            "purchasable": billing.enabled() and bool(p.stripe_price),
        }
        for p in billing.PLANS.values()
    ]


class BillingSerializer(serializers.Serializer):
    enabled = serializers.BooleanField(help_text="False on a self-hosted install: no plans, no limits.")
    plan = serializers.CharField()
    status = serializers.CharField(allow_blank=True)
    renews_at = serializers.DateTimeField(allow_null=True)
    has_billing_account = serializers.BooleanField()
    usage = serializers.DictField(child=serializers.IntegerField())
    plans = PlanSerializer(many=True)


_url = inline_serializer("BillingRedirect", {"url": serializers.URLField()})


class BillingView(APIView):
    """GET -> your plan, what you're using, and the plans on offer."""

    permission_classes = [IsAuthenticated]

    @extend_schema(responses=BillingSerializer)
    def get(self, request):
        user = request.user
        return Response(
            {
                "enabled": billing.enabled(),
                "plan": user.plan,
                "status": user.plan_status,
                "renews_at": user.plan_renews_at,
                "has_billing_account": bool(user.stripe_customer_id),
                "usage": billing.usage(user),
                "plans": _plans(),
            }
        )


class CheckoutView(APIView):
    """POST {plan} -> a Stripe Checkout URL to send the browser to."""

    permission_classes = [IsAuthenticated, NoPersonalToken]

    @extend_schema(
        request=inline_serializer(
            "CheckoutRequest", {"plan": serializers.ChoiceField(choices=[User.PLAN_CREATOR, User.PLAN_PRO])}
        ),
        responses=_url,
    )
    def post(self, request):
        if not billing.enabled():
            raise ValidationError({"detail": "Billing isn't set up on this server."})
        try:
            return Response({"url": billing.checkout_url(request.user, request.data.get("plan", ""))})
        except billing.StripeError as exc:
            raise ValidationError({"detail": str(exc)}) from exc


class PortalView(APIView):
    """POST -> a Stripe Customer Portal URL (change plan, card, invoices, cancel)."""

    permission_classes = [IsAuthenticated, NoPersonalToken]

    @extend_schema(request=None, responses=_url)
    def post(self, request):
        if not billing.enabled():
            raise ValidationError({"detail": "Billing isn't set up on this server."})
        try:
            return Response({"url": billing.portal_url(request.user)})
        except billing.StripeError as exc:
            raise ValidationError({"detail": str(exc)}) from exc


class StripeWebhookView(APIView):
    """Stripe -> us. Signed with STRIPE_WEBHOOK_SECRET; anything unsigned is
    refused. Subscribe to checkout.session.completed and
    customer.subscription.created/updated/deleted."""

    permission_classes = [AllowAny]
    authentication_classes: list[type] = []
    throttle_classes: list[type] = []

    @extend_schema(exclude=True)
    def post(self, request):
        payload = request.body
        try:
            billing.verify_webhook(payload, request.headers.get("Stripe-Signature", ""))
        except billing.StripeError as exc:
            logger.warning("Rejected Stripe webhook: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        event = json.loads(payload)
        try:
            outcome = billing.handle_event(event)
        except billing.StripeError:
            # Stripe retries non-2xx responses, which is what we want here.
            logger.exception("Stripe webhook %s failed", event.get("id"))
            return Response(status=status.HTTP_502_BAD_GATEWAY)
        logger.info("Stripe webhook %s %s: %s", event.get("id"), event.get("type"), outcome)
        return Response({"received": True})


class PricingView(APIView):
    """GET -> whether to show hosted pricing, and the plans."""

    permission_classes = [AllowAny]

    @extend_schema(
        responses=inline_serializer(
            "Pricing",
            {
                "show_pricing": serializers.BooleanField(),
                "billing_enabled": serializers.BooleanField(),
                "plans": PlanSerializer(many=True),
            },
        )
    )
    def get(self, request):
        return Response(
            {"show_pricing": settings.CLOUD_PRICING_ENABLED, "billing_enabled": billing.enabled(), "plans": _plans()}
        )


class InterestSerializer(serializers.ModelSerializer):
    plan = serializers.ChoiceField(choices=[c for c, _ in User.PLAN_CHOICES])

    class Meta:
        model = PricingInterest
        fields = ["email", "plan", "note"]


class PricingInterestView(APIView):
    """POST {email, plan, note?} -> "tell me when this plan is available".
    Measures demand for the hosted plans before they're for sale."""

    permission_classes = [AllowAny]
    throttle_scope = "interest"

    @extend_schema(request=InterestSerializer, responses={201: InterestSerializer})
    def post(self, request):
        serializer = InterestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user if request.user.is_authenticated else None
        serializer.save(user=user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
