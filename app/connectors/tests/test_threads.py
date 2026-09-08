import responses

from connectors.base import ChannelCredentials, ContentPart, PublishContext, PublishState
from connectors.platforms.threads import ThreadsConnector

_GRAPH = "https://graph.threads.net/v1.0"


def _creds():
    return ChannelCredentials(values={"ACCESS_TOKEN": "tok", "THREADS_USER_ID": "user123"})


def _ctx(text="hi threads", parts=None):
    return PublishContext(
        text=text, media=[], post_kind="text", credentials=_creds(), idempotency_key="k", parts=parts
    )


def test_authorize_url_uses_managed_app(threads_app_env):
    connector = ThreadsConnector()
    auth = connector.authorize_url(redirect_uri="https://omnipost.app/callback", state="csrf")
    assert "client_id=threads-app-id" in auth.authorize_url
    assert "threads.net/oauth/authorize" in auth.authorize_url


@responses.activate
def test_exchange_code_gets_long_lived_token(threads_app_env):
    responses.add(
        responses.POST, f"{_GRAPH}/oauth/access_token", json={"access_token": "short", "user_id": 42}, status=200
    )
    responses.add(
        responses.GET, "https://graph.threads.net/access_token", json={"access_token": "long-lived"}, status=200
    )
    connector = ThreadsConnector()
    creds = connector.exchange_code(code="abc", redirect_uri="https://x/callback")
    assert creds.values == {"ACCESS_TOKEN": "long-lived", "THREADS_USER_ID": "42"}


@responses.activate
def test_text_publish_single_step():
    responses.add(responses.POST, f"{_GRAPH}/user123/threads", json={"id": "container1"}, status=200)
    responses.add(responses.POST, f"{_GRAPH}/user123/threads_publish", json={"id": "media1"}, status=200)
    connector = ThreadsConnector()
    outcome = connector.publish(_ctx(), PublishState())
    assert outcome.done is True
    assert outcome.remote_id == "media1"


@responses.activate
def test_thread_reply_chain_uses_reply_to_id():
    responses.add(responses.POST, f"{_GRAPH}/user123/threads", json={"id": "container2"}, status=200)
    responses.add(responses.POST, f"{_GRAPH}/user123/threads_publish", json={"id": "media2"}, status=200)
    connector = ThreadsConnector()
    parts = [ContentPart(text="part one"), ContentPart(text="part two")]
    state = PublishState(step="thread_part", data={"part_index": 1, "remote_ids": ["media1"]})

    outcome = connector.publish(_ctx(parts=parts), state)

    assert outcome.done is True
    assert outcome.remote_id == "media1"
    sent_body = responses.calls[0].request.body
    assert "reply_to_id=media1" in sent_body
