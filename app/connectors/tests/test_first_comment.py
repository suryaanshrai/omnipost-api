import json

import responses

from connectors.base import ChannelCredentials
from connectors.platforms import meta_oauth
from connectors.registry import get


def _creds(**values):
    return ChannelCredentials(values=values)


@responses.activate
def test_x_first_comment_is_a_reply():
    responses.add(responses.POST, "https://api.twitter.com/2/tweets", json={"data": {"id": "99"}})
    assert get("x").post_first_comment(_creds(ACCESS_TOKEN="t"), "42", "more here") == "99"
    assert json.loads(responses.calls[0].request.body) == {"text": "more here", "reply": {"in_reply_to_tweet_id": "42"}}


@responses.activate
def test_threads_first_comment_creates_and_publishes_a_reply():
    responses.add(responses.POST, "https://graph.threads.net/v1.0/u1/threads", json={"id": "c1"})
    responses.add(responses.POST, "https://graph.threads.net/v1.0/u1/threads_publish", json={"id": "m2"})
    assert get("threads").post_first_comment(_creds(ACCESS_TOKEN="t", THREADS_USER_ID="u1"), "m1", "hi") == "m2"
    assert "reply_to_id=m1" in responses.calls[0].request.body


@responses.activate
def test_facebook_and_instagram_comment_on_the_post():
    for slug, remote in (("facebook", "page_post"), ("instagram", "ig_media")):
        responses.add(responses.POST, f"{meta_oauth.GRAPH_BASE}/{remote}/comments", json={"id": f"{slug}-c"})
        got = get(slug).post_first_comment(_creds(PAGE_ACCESS_TOKEN="t"), remote, "link in comments")
        assert got == f"{slug}-c"


def test_connectors_report_first_comment_support():
    assert get("mastodon").supports_first_comment
    assert not get("discord").supports_first_comment
