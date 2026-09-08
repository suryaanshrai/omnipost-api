import responses
from rest_framework.test import APIClient

from omnipost_api.models import AppCredential, Channel


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@responses.activate
def test_oauth_start_and_complete_creates_channel(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    responses.add(
        responses.POST,
        "https://mastodon.social/api/v1/apps",
        json={"client_id": "cid", "client_secret": "csecret"},
        status=200,
    )
    start_response = client.post(
        "/oauth/start/",
        {
            "connector_slug": "mastodon",
            "workspace": workspace.pk,
            "display_name": "My Mastodon",
            "redirect_uri": "https://app.example.com/callback",
            "extra": {"instance_domain": "mastodon.social"},
        },
        format="json",
    )
    assert start_response.status_code == 200
    provider_state = start_response.data["provider_state"]
    assert "mastodon.social/oauth/authorize" in start_response.data["authorize_url"]

    responses.add(
        responses.POST,
        "https://mastodon.social/oauth/token",
        json={"access_token": "user-token", "token_type": "Bearer"},
        status=200,
    )
    complete_response = client.post(
        "/oauth/complete/",
        {
            "connector_slug": "mastodon",
            "code": "auth-code",
            "redirect_uri": "https://app.example.com/callback",
            "state": provider_state,
        },
        format="json",
    )
    assert complete_response.status_code == 201
    channel = Channel.objects.get(pk=complete_response.data["id"])
    assert channel.workspace_id == workspace.pk
    assert channel.connector_slug == "mastodon"
    assert channel.get_credentials()["ACCESS_TOKEN"] == "user-token"


def test_oauth_start_rejects_workspace_the_user_cannot_access(db, django_user_model):
    user = django_user_model.objects.create_user(username="eve", password="x")
    other_user = django_user_model.objects.create_user(username="mallory", password="x")
    other_workspace = other_user.memberships.get().workspace

    client = _client_for(user)
    response = client.post(
        "/oauth/start/",
        {
            "connector_slug": "mastodon",
            "workspace": other_workspace.pk,
            "redirect_uri": "https://app.example.com/callback",
            "extra": {"instance_domain": "mastodon.social"},
        },
        format="json",
    )
    assert response.status_code == 403


def test_oauth_start_requires_authentication(db):
    client = APIClient()
    response = client.post(
        "/oauth/start/",
        {"connector_slug": "mastodon", "workspace": 1, "redirect_uri": "https://x/callback"},
        format="json",
    )
    assert response.status_code == 401


def test_oauth_start_for_byo_app_connector_without_app_credential_is_rejected(db, django_user_model):
    user = django_user_model.objects.create_user(username="finn", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    response = client.post(
        "/oauth/start/",
        {"connector_slug": "linkedin", "workspace": workspace.pk, "redirect_uri": "https://app.example.com/callback"},
        format="json",
    )
    assert response.status_code == 400
    assert "linkedin" in response.data[0]


def test_oauth_start_for_byo_app_connector_injects_workspace_app_credential(db, django_user_model):
    user = django_user_model.objects.create_user(username="gwen", password="x")
    workspace = user.memberships.get().workspace
    credential = AppCredential.objects.create(
        workspace=workspace, connector_slug="linkedin", client_id="workspace-cid", encrypted_client_secret={}
    )
    from omnipost_api import crypto

    credential.encrypted_client_secret = crypto.encrypt("workspace-secret", aad=credential._aad())
    credential.save(update_fields=["encrypted_client_secret"])

    client = _client_for(user)
    response = client.post(
        "/oauth/start/",
        {"connector_slug": "linkedin", "workspace": workspace.pk, "redirect_uri": "https://app.example.com/callback"},
        format="json",
    )
    assert response.status_code == 200
    assert "client_id=workspace-cid" in response.data["authorize_url"]
