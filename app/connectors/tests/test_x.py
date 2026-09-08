import responses
from django.core import signing

from connectors.base import ChannelCredentials, ContentPart, MediaRef, PublishContext, PublishState
from connectors.platforms.x import XConnector

_API = "https://api.twitter.com/2"
_UPLOAD = "https://upload.twitter.com/1.1/media/upload.json"


def _creds():
    return ChannelCredentials(values={"ACCESS_TOKEN": "tok"})


def _ctx(text="hi x", media=None, parts=None):
    return PublishContext(
        text=text,
        media=media or [],
        post_kind="image" if media else "text",
        credentials=_creds(),
        idempotency_key="k",
        parts=parts,
    )


def test_requires_own_app_flag_is_set():
    assert XConnector().requires_own_app is True


def test_authorize_url_signs_code_verifier_into_state():
    connector = XConnector()
    auth = connector.authorize_url(redirect_uri="https://x/callback", state="csrf-token", client_id="cid")

    assert "code_challenge=" in auth.authorize_url
    assert "code_challenge_method=S256" in auth.authorize_url
    payload = signing.loads(auth.state, salt="connectors.x.oauth")
    assert payload["caller_state"] == "csrf-token"
    assert len(payload["code_verifier"]) >= 43

    assert connector.unwrap_caller_state(auth.state) == "csrf-token"


@responses.activate
def test_exchange_code_uses_code_verifier_and_basic_auth():
    signed_state = signing.dumps({"caller_state": "csrf", "code_verifier": "verifier123"}, salt="connectors.x.oauth")
    responses.add(
        responses.POST,
        "https://api.twitter.com/2/oauth2/token",
        json={"access_token": "atok", "refresh_token": "rtok"},
        status=200,
    )
    responses.add(responses.GET, f"{_API}/users/me", json={"data": {"id": "user1"}}, status=200)

    connector = XConnector()
    creds = connector.exchange_code(
        code="c", redirect_uri="https://x/callback", client_id="cid", client_secret="csecret", state=signed_state
    )
    assert creds.values["ACCESS_TOKEN"] == "atok"
    assert creds.values["CLIENT_ID"] == "cid"
    assert creds.external_account_id == "user1"

    sent_body = responses.calls[0].request.body
    assert "code_verifier=verifier123" in sent_body


@responses.activate
def test_text_only_tweet():
    responses.add(responses.POST, f"{_API}/tweets", json={"data": {"id": "tweet1"}}, status=201)
    connector = XConnector()
    outcome = connector.publish(_ctx(), PublishState())
    assert outcome.done is True
    assert outcome.remote_id == "tweet1"
    assert outcome.permalink == "https://x.com/i/web/status/tweet1"


@responses.activate
def test_image_media_finalizes_synchronously_then_tweets():
    responses.add(responses.GET, "https://cdn.example.com/pic.jpg", body=b"bytes", status=200)
    responses.add(responses.POST, _UPLOAD, json={"media_id_string": "media1"}, status=200)  # INIT
    responses.add(responses.POST, _UPLOAD, json={}, status=200)  # APPEND
    responses.add(responses.POST, _UPLOAD, json={"processing_info": {"state": "succeeded"}}, status=200)  # FINALIZE
    responses.add(responses.POST, f"{_API}/tweets", json={"data": {"id": "tweet2"}}, status=201)

    connector = XConnector()
    media = [MediaRef(url="https://cdn.example.com/pic.jpg", kind="image", mime_type="image/jpeg")]
    outcome = connector.publish(_ctx(media=media), PublishState())
    assert outcome.done is True
    assert outcome.remote_id == "tweet2"


@responses.activate
def test_video_media_waits_for_processing_then_tweets():
    responses.add(responses.GET, "https://cdn.example.com/vid.mp4", body=b"bytes", status=200)
    responses.add(responses.POST, _UPLOAD, json={"media_id_string": "media2"}, status=200)  # INIT
    responses.add(responses.POST, _UPLOAD, json={}, status=200)  # APPEND
    responses.add(
        responses.POST, _UPLOAD, json={"processing_info": {"state": "in_progress", "check_after_secs": 3}}, status=200
    )  # FINALIZE

    connector = XConnector()
    media = [MediaRef(url="https://cdn.example.com/vid.mp4", kind="video", mime_type="video/mp4")]
    outcome = connector.publish(_ctx(media=media), PublishState())
    assert outcome.done is False
    assert outcome.state.step == "media_processing"
    assert outcome.retry_after_s == 3

    responses.add(responses.GET, _UPLOAD, json={"processing_info": {"state": "succeeded"}}, status=200)
    responses.add(responses.POST, f"{_API}/tweets", json={"data": {"id": "tweet3"}}, status=201)
    done = connector.publish(_ctx(media=media), outcome.state)
    assert done.done is True
    assert done.remote_id == "tweet3"


@responses.activate
def test_thread_second_part_replies_to_first():
    responses.add(responses.POST, f"{_API}/tweets", json={"data": {"id": "tweet5"}}, status=201)
    connector = XConnector()
    parts = [ContentPart(text="one"), ContentPart(text="two")]
    state = PublishState(step="thread_part", data={"part_index": 1, "remote_ids": ["tweet4"]})

    outcome = connector.publish(_ctx(parts=parts), state)

    assert outcome.done is True
    assert outcome.remote_id == "tweet4"
    sent_body = responses.calls[0].request.body
    assert b"in_reply_to_tweet_id" in sent_body
    assert b"tweet4" in sent_body
