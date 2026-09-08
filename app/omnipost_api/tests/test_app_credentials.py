from rest_framework.test import APIClient

from omnipost_api.models import AppCredential


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_create_app_credential_never_echoes_client_secret(db, django_user_model):
    user = django_user_model.objects.create_user(username="alice", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    response = client.post(
        "/app-credentials/",
        {
            "workspace": workspace.pk,
            "connector_slug": "linkedin",
            "client_id": "my-linkedin-app",
            "client_secret": "super-secret",
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    assert "client_secret" not in response.data
    assert response.data["client_id"] == "my-linkedin-app"

    credential = AppCredential.objects.get(pk=response.data["id"])
    assert credential.get_client_secret() == "super-secret"
    # Raw storage never holds the plaintext secret.
    assert "super-secret" not in str(credential.encrypted_client_secret)


def test_app_credential_is_scoped_to_workspace_and_connector(db, django_user_model):
    user = django_user_model.objects.create_user(username="bob", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    client.post(
        "/app-credentials/",
        {"workspace": workspace.pk, "connector_slug": "x", "client_id": "cid1", "client_secret": "s1"},
        format="json",
    )
    duplicate = client.post(
        "/app-credentials/",
        {"workspace": workspace.pk, "connector_slug": "x", "client_id": "cid2", "client_secret": "s2"},
        format="json",
    )
    assert duplicate.status_code == 400


def test_app_credentials_scoped_to_accessible_workspaces(db, django_user_model):
    user = django_user_model.objects.create_user(username="carl", password="x")
    other_user = django_user_model.objects.create_user(username="dana", password="x")
    other_workspace = other_user.memberships.get().workspace
    AppCredential.objects.create(
        workspace=other_workspace, connector_slug="linkedin", client_id="other-cid", encrypted_client_secret={}
    )

    client = _client_for(user)
    response = client.get("/app-credentials/")
    assert response.status_code == 200
    assert response.data["results"] == []
