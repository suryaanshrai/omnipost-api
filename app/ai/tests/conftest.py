import pytest
from omnipost_api.models import Channel, Organization, Workspace


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Test Org")
    return Workspace.objects.create(organization=org, name="Test Workspace", slug="test-workspace")


@pytest.fixture
def mastodon_channel(db, workspace):
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="Mastodon")
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    return channel
