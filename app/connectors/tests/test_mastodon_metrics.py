import pytest
import responses

from connectors.base import ChannelCredentials
from connectors.errors import AuthExpired
from connectors.platforms.mastodon import MastodonConnector


def _credentials() -> ChannelCredentials:
    return ChannelCredentials(values={"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})


@responses.activate
def test_fetch_metrics_returns_normalized_counts():
    responses.add(
        responses.GET,
        "https://mastodon.social/api/v1/statuses/999",
        json={"favourites_count": 12, "reblogs_count": 3, "replies_count": 4},
        status=200,
    )
    connector = MastodonConnector()
    metrics = connector.fetch_metrics(_credentials(), "999")
    assert metrics == {"likes": 12, "shares": 3, "comments": 4, "impressions": None}


@responses.activate
def test_fetch_metrics_raises_auth_expired_on_401():
    responses.add(responses.GET, "https://mastodon.social/api/v1/statuses/999", json={}, status=401)
    connector = MastodonConnector()
    with pytest.raises(AuthExpired):
        connector.fetch_metrics(_credentials(), "999")


@responses.activate
def test_fetch_metrics_returns_empty_dict_for_deleted_status():
    responses.add(responses.GET, "https://mastodon.social/api/v1/statuses/999", json={}, status=404)
    connector = MastodonConnector()
    assert connector.fetch_metrics(_credentials(), "999") == {}
