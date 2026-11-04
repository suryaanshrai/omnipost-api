"""fetch_metrics against recorded response shapes. The live versions of these
checks are in tests/live/ (see scripts/live_test.sh)."""

import pytest
import responses

from connectors.base import ChannelCredentials, extra_scopes
from connectors.errors import AuthExpired
from connectors.platforms import meta_oauth
from connectors.registry import get


def _creds(**values):
    return ChannelCredentials(values=values)


@responses.activate
def test_x_metrics():
    responses.add(
        responses.GET,
        "https://api.twitter.com/2/tweets/42",
        json={
            "data": {
                "id": "42",
                "public_metrics": {
                    "like_count": 3,
                    "reply_count": 1,
                    "retweet_count": 2,
                    "quote_count": 1,
                    "impression_count": 90,
                },
            }
        },
    )
    m = get("x").fetch_metrics(_creds(ACCESS_TOKEN="t"), "42")
    assert (m["likes"], m["comments"], m["shares"], m["impressions"]) == (3, 1, 3, 90)


@responses.activate
def test_x_metrics_401_needs_reconnect():
    responses.add(responses.GET, "https://api.twitter.com/2/tweets/42", status=401, json={})
    with pytest.raises(AuthExpired):
        get("x").fetch_metrics(_creds(ACCESS_TOKEN="t"), "42")


@responses.activate
def test_facebook_metrics_with_and_without_insights():
    responses.add(
        responses.GET,
        f"{meta_oauth.GRAPH_BASE}/p_1",
        json={
            "reactions": {"summary": {"total_count": 7}},
            "comments": {"summary": {"total_count": 2}},
            "shares": {"count": 1},
        },
    )
    responses.add(responses.GET, f"{meta_oauth.GRAPH_BASE}/p_1/insights", status=403, json={})
    m = get("facebook").fetch_metrics(_creds(PAGE_ACCESS_TOKEN="t"), "p_1")
    assert (m["likes"], m["comments"], m["shares"], m["impressions"]) == (7, 2, 1, None)


@responses.activate
def test_instagram_metrics_uses_insights_when_allowed():
    responses.add(responses.GET, f"{meta_oauth.GRAPH_BASE}/m1", json={"like_count": 10, "comments_count": 4})
    responses.add(
        responses.GET,
        f"{meta_oauth.GRAPH_BASE}/m1/insights",
        json={"data": [{"name": "views", "values": [{"value": 500}]}, {"name": "shares", "values": [{"value": 6}]}]},
    )
    m = get("instagram").fetch_metrics(_creds(PAGE_ACCESS_TOKEN="t"), "m1")
    assert (m["likes"], m["comments"], m["shares"], m["impressions"]) == (10, 4, 6, 500)


@responses.activate
def test_threads_metrics():
    responses.add(
        responses.GET,
        "https://graph.threads.net/v1.0/th1/insights",
        json={
            "data": [
                {"name": n, "values": [{"value": v}]}
                for n, v in (("views", 100), ("likes", 9), ("replies", 2), ("reposts", 1), ("quotes", 1))
            ]
        },
    )
    m = get("threads").fetch_metrics(_creds(ACCESS_TOKEN="t"), "th1")
    assert (m["likes"], m["comments"], m["shares"], m["impressions"]) == (9, 2, 2, 100)


@responses.activate
def test_youtube_metrics():
    responses.add(
        responses.GET,
        "https://www.googleapis.com/youtube/v3/videos",
        json={"items": [{"statistics": {"viewCount": "1200", "likeCount": "40", "commentCount": "5"}}]},
    )
    m = get("youtube").fetch_metrics(_creds(ACCESS_TOKEN="t"), "vid")
    assert (m["likes"], m["comments"], m["impressions"]) == (40, 5, 1200)


@responses.activate
def test_linkedin_metrics():
    responses.add(
        responses.GET,
        "https://api.linkedin.com/rest/socialActions/urn%3Ali%3Ashare%3A1",
        json={"likesSummary": {"totalLikes": 12}, "commentsSummary": {"aggregatedTotalComments": 3}},
    )
    m = get("linkedin").fetch_metrics(_creds(ACCESS_TOKEN="t"), "urn:li:share:1")
    assert (m["likes"], m["comments"]) == (12, 3)


@responses.activate
def test_missing_permission_means_no_metrics_not_an_error():
    responses.add(responses.GET, "https://graph.threads.net/v1.0/th1/insights", status=403, json={})
    assert get("threads").fetch_metrics(_creds(ACCESS_TOKEN="t"), "th1") == {}


def test_every_major_platform_reports_metrics():
    for slug in ("x", "facebook", "instagram", "threads", "youtube", "linkedin", "bluesky", "mastodon"):
        assert get(slug).supports_metrics, slug


def test_extra_scopes(monkeypatch):
    monkeypatch.setenv("THREADS_EXTRA_SCOPES", "threads_manage_insights, threads_basic")
    assert extra_scopes("threads_basic,threads_content_publish", "THREADS_EXTRA_SCOPES", ",") == (
        "threads_basic,threads_content_publish,threads_manage_insights"
    )
    monkeypatch.delenv("THREADS_EXTRA_SCOPES")
    assert extra_scopes("a b", "THREADS_EXTRA_SCOPES") == "a b"
