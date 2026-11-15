import hashlib
import hmac
import json
from datetime import datetime
from unittest import mock

import responses
from django.core import mail
from django.test import override_settings
from django.utils import timezone
from jobs.digest import send_due_digests
from jobs.feeds import check_feed, parse_items
from jobs.utm import tag
from jobs.webhooks import deliver, sign
from rest_framework.test import APIClient

from omnipost_api.models import Channel, Feed, Membership, Post, PostTarget, Webhook


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _setup(django_user_model, name):
    user = django_user_model.objects.create_user(username=name, email=f"{name}@example.com", password="x")
    workspace = user.memberships.get().workspace
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="M")
    channel.set_credentials({"ACCESS_TOKEN": "t", "INSTANCE_DOMAIN": "mastodon.social"})
    return user, workspace, channel


# --- UTM -------------------------------------------------------------------------


def test_utm_tagging_adds_params_and_keeps_existing_ones():
    assert tag("https://ex.com/a?x=1", source="bluesky") == (
        "https://ex.com/a?x=1&utm_source=bluesky&utm_medium=social&utm_campaign=omnipost"
    )
    assert "utm_source=mine" in tag("https://ex.com/?utm_source=mine", source="x")
    assert tag("mailto:a@b.c", source="x") == "mailto:a@b.c"


def test_utm_is_applied_at_publish_only_when_switched_on(db, django_user_model):
    from jobs.context import build_context

    user, workspace, channel = _setup(django_user_model, "utm")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="x", link="https://ex.com/p")
    target = PostTarget.objects.create(post=post, channel=channel)
    assert build_context(target).link == "https://ex.com/p"
    workspace.utm_tagging = True
    workspace.save()
    target.refresh_from_db()
    assert "utm_source=mastodon" in build_context(target).link


# --- search and duplicate -----------------------------------------------------------


def test_search_and_duplicate(db, django_user_model):
    user, workspace, channel = _setup(django_user_model, "dup")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="Launch day!", link="https://ex.com")
    PostTarget.objects.create(post=post, channel=channel, text_override="Launch on Mastodon")
    Post.objects.create(workspace=workspace, kind="text", base_text="Something else")
    client = _client_for(user)

    found = client.get(f"/posts/?workspace={workspace.pk}&q=launch").data["results"]
    assert [p["id"] for p in found] == [post.pk]

    copy = client.post(f"/posts/{post.pk}/duplicate/").data
    assert copy["status"] == "draft" and copy["id"] != post.pk
    assert copy["base_text"] == "Launch day!"
    assert copy["targets"][0]["channel"] == channel.pk
    assert copy["targets"][0]["text_override"] == "Launch on Mastodon"


# --- webhooks -------------------------------------------------------------------------


def test_webhook_secret_is_shown_once_and_url_must_be_public_https(db, django_user_model):
    user, workspace, _ = _setup(django_user_model, "hook")
    client = _client_for(user)
    bad = client.post("/webhooks/", {"workspace": workspace.pk, "url": "http://127.0.0.1/x"}, format="json")
    assert bad.status_code == 400
    created = client.post("/webhooks/", {"workspace": workspace.pk, "url": "https://93.184.216.34/hook"}, format="json")
    assert created.status_code == 201 and len(created.data["secret"]) == 64
    listed = client.get(f"/webhooks/?workspace={workspace.pk}").data["results"][0]
    assert "secret" not in listed


@responses.activate
def test_webhook_delivery_is_signed(db, django_user_model):
    _, workspace, _ = _setup(django_user_model, "sig")
    hook = Webhook.objects.create(workspace=workspace, url="https://93.184.216.34/hook", secret="s3cret")
    responses.add(responses.POST, "https://93.184.216.34/hook", status=204)
    deliver(hook.pk, {"event": "post.published", "workspace": workspace.pk, "data": {"post": 1}})

    request = responses.calls[0].request
    ts = request.headers["X-OmniPost-Timestamp"]
    expected = "sha256=" + hmac.new(b"s3cret", f"{ts}.".encode() + request.body, hashlib.sha256).hexdigest()
    assert request.headers["X-OmniPost-Signature"] == expected == sign("s3cret", ts, request.body)
    assert json.loads(request.body)["event"] == "post.published"
    hook.refresh_from_db()
    assert hook.last_status == "204"


@responses.activate
def test_publishing_emits_a_webhook(db, django_user_model, enqueued, monkeypatch):
    from jobs.publisher import run_publish_attempt
    from jobs.scheduler import schedule_post_target

    user, workspace, channel = _setup(django_user_model, "emit")
    Webhook.objects.create(workspace=workspace, url="https://93.184.216.34/h", secret="s", events=["post.published"])
    sent = []
    monkeypatch.setattr("jobs.queue.enqueue_webhook", lambda hook_id, payload: sent.append(payload["event"]))
    responses.add(responses.POST, "https://mastodon.social/api/v1/statuses", json={"id": "1", "url": "u"})
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    target = PostTarget.objects.create(post=post, channel=channel)
    with mock.patch("django.db.transaction.on_commit", side_effect=lambda fn: fn()):
        run_publish_attempt(schedule_post_target(target).pk)
    assert sent == ["post.published"]


# --- feeds -------------------------------------------------------------------------------

_RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Second</title><link>https://blog.ex/2</link><guid>2</guid></item>
<item><title>First</title><link>https://blog.ex/1</link><guid>1</guid></item>
</channel></rss>"""

_ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><id>a1</id><title>Atom one</title><link rel="alternate" href="https://ex.com/a1"/></entry></feed>"""


def test_parse_rss_and_atom():
    assert [i["title"] for i in parse_items(_RSS)] == ["Second", "First"]
    assert parse_items(_ATOM) == [{"id": "a1", "title": "Atom one", "link": "https://ex.com/a1"}]


@responses.activate
def test_feed_makes_drafts_only_for_items_after_the_first_check(db, django_user_model):
    user, workspace, channel = _setup(django_user_model, "feed")
    feed = Feed.objects.create(workspace=workspace, url="https://93.184.216.34/feed.xml")
    feed.channels.set([channel])
    responses.add(responses.GET, "https://93.184.216.34/feed.xml", body=_RSS)
    assert check_feed(feed) == 0  # first look: remember, don't flood Drafts

    newer = _RSS.replace(
        "<item><title>Second",
        "<item><title>Third</title><link>https://blog.ex/3</link><guid>3</guid></item><item><title>Second",
    )
    responses.replace(responses.GET, "https://93.184.216.34/feed.xml", body=newer)
    assert check_feed(feed) == 1
    draft = Post.objects.get(workspace=workspace)
    assert draft.status == "draft" and draft.base_text == "Third https://blog.ex/3"
    assert list(draft.targets.values_list("channel_id", flat=True)) == [channel.pk]
    assert check_feed(feed) == 0


def test_feed_refuses_internal_urls(db, django_user_model):
    user, workspace, _ = _setup(django_user_model, "feed2")
    r = _client_for(user).post("/feeds/", {"workspace": workspace.pk, "url": "http://10.0.0.1/rss"}, format="json")
    assert r.status_code == 400


# --- digest --------------------------------------------------------------------------


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_monday_digest_summarises_the_week_once(db, django_user_model):
    user, workspace, channel = _setup(django_user_model, "dig")
    other = django_user_model.objects.create_user(username="quiet", email="q@example.com", password="x")
    Membership.objects.filter(user=other).update(role="owner")
    post = Post.objects.create(workspace=workspace, kind="text", base_text="x")
    target = PostTarget.objects.create(post=post, channel=channel, status="published")
    monday = timezone.make_aware(datetime(2030, 1, 7, 9, 0))
    PostTarget.objects.filter(pk=target.pk).update(updated_at=monday - timezone.timedelta(days=2))
    with mock.patch("django.utils.timezone.now", return_value=monday):
        assert send_due_digests(monday) == {"sent": 1}  # "quiet" had nothing, so no email
        assert send_due_digests(monday) == {"sent": 0}
    assert "1 delivered" in mail.outbox[0].body
    tuesday = timezone.make_aware(datetime(2030, 1, 8, 9, 0))
    assert send_due_digests(tuesday) == {"sent": 0}
