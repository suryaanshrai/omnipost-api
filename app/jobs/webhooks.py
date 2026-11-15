"""Outbound webhooks: a signed JSON POST per event to each matching hook.

Signature: X-OmniPost-Signature: sha256=<hex HMAC of "<timestamp>.<body>">,
with the timestamp in X-OmniPost-Timestamp, so receivers can reject replays.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import requests
from connectors.netguard import UnsafeURL, assert_public_url
from django.db import transaction
from django.utils import timezone
from omnipost_api.models import Webhook

logger = logging.getLogger(__name__)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def emit(workspace_id: int, event: str, data: dict) -> None:
    """Queue one delivery per active hook subscribed to `event`, after commit."""
    hook_ids = [
        h.pk
        for h in Webhook.objects.filter(workspace_id=workspace_id, is_active=True)
        if event in (h.events or Webhook.EVENTS)
    ]
    if not hook_ids:
        return
    from . import queue as job_queue

    payload = {"event": event, "workspace": workspace_id, "data": data, "sent_at": timezone.now().isoformat()}

    def send() -> None:
        for hook_id in hook_ids:
            job_queue.enqueue_webhook(hook_id, payload)

    transaction.on_commit(send)


def deliver(hook_id: int, payload: dict) -> None:
    hook = Webhook.objects.filter(pk=hook_id, is_active=True).first()
    if hook is None:
        return
    body = json.dumps(payload, separators=(",", ":"), default=str).encode()
    timestamp = str(int(time.time()))
    try:
        assert_public_url(hook.url, schemes=("https",))
        response = requests.post(
            hook.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "OmniPost-Webhooks/1.0",
                "X-OmniPost-Event": payload["event"],
                "X-OmniPost-Timestamp": timestamp,
                "X-OmniPost-Signature": sign(hook.secret, timestamp, body),
            },
            timeout=10,
            allow_redirects=False,
        )
        status = f"{response.status_code}"
    except UnsafeURL as exc:
        status = f"refused: {exc}"
    except requests.RequestException as exc:
        status = f"error: {type(exc).__name__}"
    hook.last_status = status[:100]
    hook.last_delivered_at = timezone.now()
    hook.save(update_fields=["last_status", "last_delivered_at"])
