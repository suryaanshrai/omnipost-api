"""Live checks against the real platforms. Never run in CI.

Each test skips until its credentials are set (see .env.live.example), then:
  * reads metrics for a post you already have (read-only), and
  * with LIVE_PUBLISH=1, publishes a short test post (and, with
    LIVE_FIRST_COMMENT=1, replies to it). This creates real posts.

Run:  ./scripts/live_test.sh               (all platforms with credentials)
      ./scripts/live_test.sh -k x          (just X)
"""

from __future__ import annotations

import os
import time

import pytest

from connectors.base import ChannelCredentials, PublishContext, PublishState
from connectors.registry import get

pytestmark = pytest.mark.live

# slug: (credential field -> env var, the env var naming an existing post, permission needed for metrics)
PLATFORMS: dict[str, tuple[dict[str, str], str, str]] = {
    "x": ({"ACCESS_TOKEN": "LIVE_X_ACCESS_TOKEN"}, "LIVE_X_POST_ID", "tweet.read"),
    "linkedin": (
        {"ACCESS_TOKEN": "LIVE_LINKEDIN_ACCESS_TOKEN", "MEMBER_URN": "LIVE_LINKEDIN_MEMBER_URN"},
        "LIVE_LINKEDIN_POST_URN",
        "r_member_social (set LINKEDIN_EXTRA_SCOPES and reconnect)",
    ),
    "facebook": (
        {"PAGE_ID": "LIVE_FACEBOOK_PAGE_ID", "PAGE_ACCESS_TOKEN": "LIVE_FACEBOOK_PAGE_ACCESS_TOKEN"},
        "LIVE_FACEBOOK_POST_ID",
        "pages_read_engagement",
    ),
    "instagram": (
        {"IG_USER_ID": "LIVE_INSTAGRAM_IG_USER_ID", "PAGE_ACCESS_TOKEN": "LIVE_INSTAGRAM_PAGE_ACCESS_TOKEN"},
        "LIVE_INSTAGRAM_MEDIA_ID",
        "instagram_basic (instagram_manage_insights for reach/views)",
    ),
    "threads": (
        {"THREADS_USER_ID": "LIVE_THREADS_USER_ID", "ACCESS_TOKEN": "LIVE_THREADS_ACCESS_TOKEN"},
        "LIVE_THREADS_POST_ID",
        "threads_manage_insights (set THREADS_EXTRA_SCOPES and reconnect)",
    ),
    "youtube": (
        {"ACCESS_TOKEN": "LIVE_YOUTUBE_ACCESS_TOKEN"},
        "LIVE_YOUTUBE_VIDEO_ID",
        "youtube.readonly (set YOUTUBE_EXTRA_SCOPES and reconnect)",
    ),
    "bluesky": (
        {"IDENTIFIER": "LIVE_BLUESKY_IDENTIFIER", "APP_PASSWORD": "LIVE_BLUESKY_APP_PASSWORD"},
        "LIVE_BLUESKY_POST_URI",
        "an app password",
    ),
    "mastodon": (
        {"INSTANCE_DOMAIN": "LIVE_MASTODON_INSTANCE_DOMAIN", "ACCESS_TOKEN": "LIVE_MASTODON_ACCESS_TOKEN"},
        "LIVE_MASTODON_STATUS_ID",
        "read:statuses",
    ),
}

# Text-only publishing works on these; Instagram/YouTube/TikTok need media.
TEXT_PUBLISHABLE = {"x", "linkedin", "facebook", "threads", "bluesky", "mastodon"}
# Publish-only platforms (no metrics API).
PUBLISH_ONLY = {
    "discord": {"WEBHOOK_URL": "LIVE_DISCORD_WEBHOOK_URL"},
    "telegram": {"BOT_TOKEN": "LIVE_TELEGRAM_BOT_TOKEN", "CHAT_ID": "LIVE_TELEGRAM_CHAT_ID"},
}


def _credentials(fields: dict[str, str]) -> ChannelCredentials:
    missing = [env for env in fields.values() if not os.environ.get(env)]
    if missing:
        pytest.skip(f"set {', '.join(missing)} in .env.live")
    return ChannelCredentials(values={field: os.environ[env] for field, env in fields.items()})


def _publish(slug: str, credentials: ChannelCredentials, text: str):
    connector = get(slug)
    ctx = PublishContext(
        text=text, media=[], post_kind="text", credentials=credentials, idempotency_key=f"live-{time.time()}"
    )
    state = PublishState()
    deadline = time.time() + 180
    while True:
        outcome = connector.publish(ctx, state)
        if outcome.done:
            return outcome
        assert time.time() < deadline, f"{slug}: still processing after 3 minutes"
        state = outcome.state or state
        time.sleep(min(outcome.retry_after_s or 5, 15))


@pytest.mark.parametrize("slug", sorted(PLATFORMS))
def test_metrics(slug):
    fields, post_env, permission = PLATFORMS[slug]
    credentials = _credentials(fields)
    post_id = os.environ.get(post_env)
    if not post_id:
        pytest.skip(f"set {post_env} to one of your existing posts")
    metrics = get(slug).fetch_metrics(credentials, post_id)
    assert metrics, f"{slug} returned no metrics for {post_id}. Check the post id and that the token has {permission}."
    print(f"\n{slug} metrics: {metrics}")
    assert any(metrics.get(k) is not None for k in ("likes", "comments", "shares", "impressions"))


@pytest.mark.parametrize("slug", sorted(TEXT_PUBLISHABLE | set(PUBLISH_ONLY)))
def test_publish_text(slug):
    if os.environ.get("LIVE_PUBLISH") != "1":
        pytest.skip("set LIVE_PUBLISH=1 to publish a real test post")
    fields = PUBLISH_ONLY.get(slug) or PLATFORMS[slug][0]
    credentials = _credentials(fields)
    outcome = _publish(slug, credentials, f"OmniPost live test {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"\n{slug} published: {outcome.remote_id} {outcome.permalink or ''}")
    assert outcome.remote_id

    connector = get(slug)
    if os.environ.get("LIVE_FIRST_COMMENT") == "1" and connector.supports_first_comment:
        comment_id = connector.post_first_comment(credentials, outcome.remote_id, "OmniPost live test: first comment")
        print(f"{slug} first comment: {comment_id}")
        assert comment_id


@pytest.mark.parametrize("slug", ["x", "threads", "youtube", "tiktok"])
def test_token_refresh(slug):
    """For platforms whose tokens rotate: proves the refresh token works."""
    env = f"LIVE_{slug.upper()}_REFRESH_TOKEN"
    if not os.environ.get(env):
        pytest.skip(f"set {env} (and the platform's app keys) to test refresh")
    fields = {"REFRESH_TOKEN": env, "ACCESS_TOKEN": f"LIVE_{slug.upper()}_ACCESS_TOKEN"}
    if slug == "x":
        fields |= {"CLIENT_ID": "LIVE_X_CLIENT_ID", "CLIENT_SECRET": "LIVE_X_CLIENT_SECRET"}
    refreshed = get(slug).refresh(_credentials(fields))
    assert refreshed.get("ACCESS_TOKEN")
    print(f"\n{slug}: refreshed, new token expires {refreshed.get('EXPIRES_AT') or 'unknown'}")
