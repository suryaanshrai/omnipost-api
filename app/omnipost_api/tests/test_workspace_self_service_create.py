"""POST /workspaces/ with only `name` used to 400 on "organization: This
field is required." before perform_create's own fallback (an auto-created
Organization) ever ran — the model field has no null=True/blank=True, so
ModelSerializer marked it required by default, and `slug` had the same
problem. That made WorkspaceViewSet.perform_create's fallback branch dead
code and self-service "create my first workspace" impossible via the API.
These pin the fix, mirroring services/provisioning.py's own signup-time
Organization+slug derivation (see unique_workspace_slug)."""

from rest_framework.test import APIClient

from omnipost_api.models import Membership, Organization, Workspace


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_create_workspace_with_only_a_name(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")

    response = _client_for(user).post("/workspaces/", {"name": "Side Project"}, format="json")

    assert response.status_code == 201, response.data
    workspace = Workspace.objects.get(pk=response.data["id"])
    assert workspace.name == "Side Project"
    assert workspace.slug  # derived, non-empty
    assert Organization.objects.filter(pk=workspace.organization_id).exists()
    assert Membership.objects.filter(workspace=workspace, user=user, role=Membership.ROLE_OWNER).exists()


def test_create_workspace_derives_a_unique_slug_on_a_name_collision(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    client = _client_for(user)

    first = client.post("/workspaces/", {"name": "Side Project"}, format="json")
    second = client.post("/workspaces/", {"name": "Side Project"}, format="json")

    assert first.status_code == 201, first.data
    assert second.status_code == 201, second.data
    assert first.data["slug"] != second.data["slug"]


def test_create_workspace_rejects_a_slug_already_in_use(db, django_user_model):
    user = django_user_model.objects.create_user(username="dana", password="x")
    existing_workspace = user.memberships.get().workspace  # signup's own auto-provisioned workspace

    response = _client_for(user).post(
        "/workspaces/", {"name": "Another Name", "slug": existing_workspace.slug}, format="json"
    )

    assert response.status_code == 400
    assert "slug" in response.data
