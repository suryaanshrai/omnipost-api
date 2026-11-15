"""Telegram via the Bot API: the user creates a bot with @BotFather and adds
it to their channel or group. There's no OAuth for this API."""

from __future__ import annotations

import requests

from ..base import Connector, PublishContext, PublishOutcome, PublishState
from ..capabilities import Capabilities
from ..http import json_or_empty, raise_for_platform


class TelegramConnector(Connector):
    slug = "telegram"
    credential_fields = ("BOT_TOKEN", "CHAT_ID")
    capabilities = Capabilities(slug="telegram", display_name="Telegram", max_text_length=4096, post_kinds=("text",))

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        response = requests.post(
            f"https://api.telegram.org/bot{ctx.credentials['BOT_TOKEN']}/sendMessage",
            json={"chat_id": ctx.credentials["CHAT_ID"], "text": ctx.text},
            timeout=30,
        )
        raise_for_platform(response, "Telegram")
        message_id = (json_or_empty(response).get("result") or {}).get("message_id")
        return PublishOutcome(done=True, remote_id=str(message_id) if message_id is not None else None)
