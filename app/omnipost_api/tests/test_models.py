import pytest

from omnipost_api.models import Post, PostTarget


def test_migrations_apply_and_schema_is_queryable(db):
    # If the migration graph or model definitions were broken, this fixture
    # (which runs migrate against sqlite) would have already failed before
    # this line. This just confirms basic querying works post-migration.
    assert list(Post.objects.all()) == []


def test_signup_auto_provisions_personal_workspace(db, django_user_model):
    user = django_user_model.objects.create_user(username="bob", password="x")
    from omnipost_api.models import Membership

    membership = Membership.objects.get(user=user)
    assert membership.role == Membership.ROLE_OWNER
    assert membership.workspace.organization.plan == "free"


def test_channel_credentials_round_trip(mastodon_channel):
    creds = mastodon_channel.get_credentials()
    assert creds == {"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"}


def test_channel_credentials_are_never_stored_in_plaintext(mastodon_channel):
    blob = mastodon_channel.credential.encrypted_values
    assert "tok" not in str(blob)
    assert "mastodon.social" not in str(blob)


def test_post_target_effective_text_falls_back_to_post_base_text(workspace, mastodon_channel):
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hello from post")
    target = PostTarget.objects.create(post=post, channel=mastodon_channel)
    assert target.effective_text() == "hello from post"

    target.text_override = "custom per-channel text"
    target.save()
    assert target.effective_text() == "custom per-channel text"


def test_post_target_idempotency_key_changes_with_epoch(workspace, mastodon_channel):
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hi")
    target = PostTarget.objects.create(post=post, channel=mastodon_channel)
    key1 = target.idempotency_key()
    target.epoch += 1
    target.save()
    key2 = target.idempotency_key()
    assert key1 != key2


def test_duplicate_membership_rejected(db, django_user_model):
    from django.db import IntegrityError

    from omnipost_api.models import Membership

    carol = django_user_model.objects.create_user(username="carol", password="x")
    workspace = carol.memberships.get().workspace  # auto-provisioned on signup
    with pytest.raises(IntegrityError):
        Membership.objects.create(workspace=workspace, user=carol, role=Membership.ROLE_VIEWER)
