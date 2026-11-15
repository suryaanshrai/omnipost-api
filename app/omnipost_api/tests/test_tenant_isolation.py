"""Regression tests for workspace isolation and role enforcement.

Each test is one way a member of workspace A used to be able to read or
change workspace B, or a low-privilege member to act above their role.
"""

import pytest
import responses
from rest_framework.test import APIClient

from omnipost_api.models import (
    Channel,
    MediaAsset,
    Membership,
    Organization,
    Post,
    QueueSlot,
    RecurrenceRule,
    Workspace,
)


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.fixture
def alice(django_user_model):
    return django_user_model.objects.create_user(username="alice2", email="alice2@example.com", password="x")


@pytest.fixture
def bob(django_user_model):
    return django_user_model.objects.create_user(username="bob2", email="bob2@example.com", password="x")


def _ws(user):
    return user.memberships.get().workspace


def _channel(workspace, name="Chan"):
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name=name)
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    return channel


def _member(user, workspace, role):
    return Membership.objects.create(user=user, workspace=workspace, role=role)


# --- cross-workspace writes ------------------------------------------------


def test_member_cannot_move_their_membership_into_another_workspace(db, alice, bob):
    mine = alice.memberships.get()
    response = _client_for(alice).patch(
        f"/memberships/{mine.pk}/", {"workspace": _ws(bob).pk, "role": "owner"}, format="json"
    )
    mine.refresh_from_db()
    assert mine.workspace_id == _ws(alice).pk
    assert not Membership.objects.filter(user=alice, workspace=_ws(bob)).exists()
    assert response.status_code in (200, 400, 403)


def test_post_cannot_target_another_workspaces_channel(db, alice, bob, enqueued):
    foreign = _channel(_ws(bob))
    response = _client_for(alice).post(
        "/posts/",
        {"workspace": _ws(alice).pk, "kind": "text", "base_text": "hi", "target_specs": [{"channel": foreign.pk}]},
        format="json",
    )
    assert response.status_code == 400
    assert not Post.objects.exists()


def test_post_cannot_mix_channels_from_two_of_my_workspaces(db, alice, enqueued):
    first = _ws(alice)
    second = Workspace.objects.create(organization=Organization.objects.create(name="o2"), name="Two", slug="two")
    _member(alice, second, Membership.ROLE_OWNER)
    other = _channel(second)
    response = _client_for(alice).post(
        "/posts/",
        {"workspace": first.pk, "kind": "text", "base_text": "hi", "target_specs": [{"channel": other.pk}]},
        format="json",
    )
    assert response.status_code == 400


def test_post_cannot_attach_another_workspaces_media(db, alice, bob):
    asset = MediaAsset.objects.create(workspace=_ws(bob), kind="image", file="media/x.jpg")
    response = _client_for(alice).post(
        "/posts/",
        {"workspace": _ws(alice).pk, "kind": "image", "base_text": "hi", "base_media": [asset.pk]},
        format="json",
    )
    assert response.status_code == 400


def test_patch_cannot_move_a_post_to_another_workspace(db, alice, bob):
    post = Post.objects.create(workspace=_ws(alice), kind="text", base_text="hi")
    _client_for(alice).patch(f"/posts/{post.pk}/", {"workspace": _ws(bob).pk}, format="json")
    post.refresh_from_db()
    assert post.workspace_id == _ws(alice).pk


def test_queue_slot_cannot_be_created_on_another_workspaces_channel(db, alice, bob):
    foreign = _channel(_ws(bob))
    response = _client_for(alice).post(
        "/queue-slots/", {"channel": foreign.pk, "weekday": 0, "time_of_day": "09:00"}, format="json"
    )
    assert response.status_code == 400
    assert not QueueSlot.objects.exists()


def test_recurrence_rule_cannot_use_another_workspaces_channel(db, alice, bob):
    foreign = _channel(_ws(bob))
    response = _client_for(alice).post(
        "/recurrence-rules/",
        {
            "workspace": _ws(alice).pk,
            "channel": foreign.pk,
            "kind": "text",
            "variants": ["a"],
            "interval_hours": 24,
            "next_run_at": "2030-01-01T00:00:00Z",
        },
        format="json",
    )
    assert response.status_code == 400
    assert not RecurrenceRule.objects.exists()


def test_new_workspace_ignores_a_supplied_organization(db, alice, bob):
    bobs_org = _ws(bob).organization
    response = _client_for(alice).post("/workspaces/", {"name": "Sneaky", "organization": bobs_org.pk}, format="json")
    assert response.status_code == 201
    assert Workspace.objects.get(pk=response.data["id"]).organization_id != bobs_org.pk


# --- roles -----------------------------------------------------------------


def test_viewer_cannot_write(db, alice, bob, enqueued):
    workspace = _ws(alice)
    _member(bob, workspace, Membership.ROLE_VIEWER)
    channel = _channel(workspace)
    client = _client_for(bob)

    assert (
        client.post("/posts/", {"workspace": workspace.pk, "kind": "text", "base_text": "x"}, format="json").status_code
        == 403
    )
    assert client.patch(f"/channels/{channel.pk}/", {"display_name": "pwned"}, format="json").status_code == 403
    assert client.delete(f"/workspaces/{workspace.pk}/").status_code == 403
    assert Workspace.objects.filter(pk=workspace.pk).exists()
    # Reads still work.
    assert client.get(f"/posts/?workspace={workspace.pk}").status_code == 200


def test_contributor_can_draft_but_not_schedule(db, alice, bob, enqueued):
    workspace = _ws(alice)
    _member(bob, workspace, Membership.ROLE_CONTRIBUTOR)
    channel = _channel(workspace)
    client = _client_for(bob)

    created = client.post(
        "/posts/",
        {"workspace": workspace.pk, "kind": "text", "base_text": "x", "target_specs": [{"channel": channel.pk}]},
        format="json",
    )
    assert created.status_code == 201
    assert client.post(f"/posts/{created.data['id']}/schedule/", {}, format="json").status_code == 403


def test_client_reviewer_can_approve_but_contributor_cannot(db, alice, bob, django_user_model):
    workspace = _ws(alice)
    _member(bob, workspace, Membership.ROLE_CLIENT_REVIEWER)
    carol = django_user_model.objects.create_user(username="carol2", password="x")
    _member(carol, workspace, Membership.ROLE_CONTRIBUTOR)
    post = Post.objects.create(workspace=workspace, kind="text", base_text="x", status=Post.STATUS_IN_REVIEW)

    assert _client_for(carol).post(f"/posts/{post.pk}/approve/").status_code == 403
    assert _client_for(bob).post(f"/posts/{post.pk}/approve/").status_code == 200


def test_only_admins_add_members_and_nobody_grants_above_their_role(db, alice, bob, django_user_model):
    workspace = _ws(alice)
    carol = django_user_model.objects.create_user(username="carol3", email="carol3@example.com", password="x")
    _member(bob, workspace, Membership.ROLE_ADMIN)

    owner_grant = _client_for(bob).post(
        "/memberships/", {"workspace": workspace.pk, "username": "carol3", "role": "owner"}, format="json"
    )
    assert owner_grant.status_code == 403

    added = _client_for(bob).post(
        "/memberships/", {"workspace": workspace.pk, "username": "carol3@example.com", "role": "editor"}, format="json"
    )
    assert added.status_code == 201, added.data
    assert Membership.objects.get(user=carol, workspace=workspace).role == "editor"

    # An editor can't add people.
    dan = django_user_model.objects.create_user(username="dan3", password="x")
    assert (
        _client_for(carol)
        .post("/memberships/", {"workspace": workspace.pk, "username": dan.username}, format="json")
        .status_code
        == 403
    )


def test_last_owner_cannot_be_demoted(db, alice):
    mine = alice.memberships.get()
    response = _client_for(alice).patch(f"/memberships/{mine.pk}/", {"role": "editor"}, format="json")
    assert response.status_code == 400
    mine.refresh_from_db()
    assert mine.role == Membership.ROLE_OWNER


# --- OAuth -------------------------------------------------------------------


@responses.activate
def test_oauth_complete_with_someone_elses_state_creates_nothing(db, alice, bob):
    responses.add(responses.POST, "https://mastodon.social/api/v1/apps", json={"client_id": "c", "client_secret": "s"})
    start = _client_for(bob).post(
        "/oauth/start/",
        {
            "connector_slug": "mastodon",
            "workspace": _ws(bob).pk,
            "redirect_uri": "https://app.example.com/cb",
            "extra": {"instance_domain": "mastodon.social"},
        },
        format="json",
    )
    assert start.status_code == 200
    responses.add(responses.POST, "https://mastodon.social/oauth/token", json={"access_token": "t"})

    response = _client_for(alice).post(
        "/oauth/complete/",
        {
            "connector_slug": "mastodon",
            "code": "c",
            "redirect_uri": "https://app.example.com/cb",
            "state": start.data["provider_state"],
        },
        format="json",
    )
    assert response.status_code in (400, 403)
    assert not Channel.objects.exists()
    # The code was never exchanged.
    assert not any(call.request.url.endswith("/oauth/token") for call in responses.calls)
