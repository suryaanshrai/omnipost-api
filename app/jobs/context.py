"""Builds a connectors.base.PublishContext from a PostTarget row — the
adapter between the Django model layer and the connector framework, which
deliberately knows nothing about Django."""

from __future__ import annotations

from connectors.base import ChannelCredentials, ContentPart, MediaRef, PublishContext
from django.conf import settings
from omnipost_api.models import MediaAsset, PostTarget


def media_ref(asset: MediaAsset) -> MediaRef:
    url = asset.file.url
    if url.startswith("/"):
        # Local FileSystemStorage in dev returns a relative URL; connectors
        # need an absolute one to fetch or hand to a platform. In prod,
        # S3-backed storage (S3_MEDIA_ENABLED) already returns an absolute URL.
        base = getattr(settings, "PUBLIC_BASE_URL", "http://localhost:8000")
        url = base.rstrip("/") + url
    return MediaRef(
        url=url,
        kind=asset.kind,
        mime_type=asset.mime_type or "application/octet-stream",
        alt_text=asset.alt_text or None,
        duration_s=asset.duration_s,
        width=asset.width,
        height=asset.height,
        size_bytes=asset.size_bytes,
    )


def build_context(post_target: PostTarget) -> PublishContext:
    channel = post_target.channel
    credentials = ChannelCredentials(values=channel.get_credentials(), external_account_id=channel.external_account_id)
    parts = [
        ContentPart(
            text=part.text,
            media=[media_ref(asset) for asset in part.media.all()],
            delay_after_s=part.delay_after_s,
        )
        for part in post_target.parts.order_by("sequence")
    ]
    return PublishContext(
        text=post_target.effective_text(),
        media=[media_ref(asset) for asset in post_target.effective_media()],
        post_kind=post_target.effective_format(),
        credentials=credentials,
        idempotency_key=post_target.idempotency_key(),
        link=post_target.post.link or None,
        parts=parts or None,
    )
