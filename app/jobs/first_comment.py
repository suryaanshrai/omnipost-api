"""Posts a target's first comment after the post itself is published. Runs
as its own job so a slow or failed comment never holds up, or fails, the
post."""

from __future__ import annotations

import logging

import requests
from connectors.base import ChannelCredentials
from connectors.errors import ConnectorError
from connectors.registry import get as get_connector
from omnipost_api.models import PostTarget

logger = logging.getLogger(__name__)


def post_first_comment(post_target_id: int) -> None:
    target = PostTarget.objects.select_related("channel").filter(pk=post_target_id).first()
    if target is None or not target.first_comment or not target.remote_id or target.first_comment_result:
        return
    connector = get_connector(target.channel.connector_slug)
    if not connector.supports_first_comment:
        target.first_comment_result = "This platform doesn't support a first comment."
    else:
        credentials = ChannelCredentials(
            values=target.channel.get_credentials(), external_account_id=target.channel.external_account_id
        )
        try:
            connector.post_first_comment(credentials, target.remote_id, target.first_comment)
            target.first_comment_result = "posted"
        except ConnectorError as exc:
            target.first_comment_result = f"Not posted: {exc.safe_detail}"[:300]
        except requests.RequestException:
            target.first_comment_result = "Not posted: couldn't reach the platform."
    target.save(update_fields=["first_comment_result", "updated_at"])
