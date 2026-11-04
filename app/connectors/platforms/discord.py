"""Discord via an incoming webhook: the user pastes a webhook URL from a
channel's Integrations settings. No OAuth app or bot install needed."""

from __future__ import annotations

import requests

from ..base import Connector, PublishContext, PublishOutcome, PublishState
from ..capabilities import Capabilities
from ..http import json_or_empty, raise_for_platform


class DiscordConnector(Connector):
    slug = "discord"
    credential_fields = ("WEBHOOK_URL",)
    capabilities = Capabilities(slug="discord", display_name="Discord", max_text_length=2000, post_kinds=("text",))

    def publish(self, ctx: PublishContext, state: PublishState) -> PublishOutcome:
        response = requests.post(
            ctx.credentials["WEBHOOK_URL"],
            params={"wait": "true"},  # return the created message, so we get its id
            json={"content": ctx.text},
            timeout=30,
        )
        # A deleted webhook is a 404: the channel needs a new URL.
        raise_for_platform(response, "Discord", auth_statuses=(401, 403, 404))
        return PublishOutcome(done=True, remote_id=str(json_or_empty(response).get("id") or "") or None)
