import pytest
from omnipost_api.models import Channel, Organization, Post, PostTarget, Workspace


@pytest.fixture
def enqueued(monkeypatch):
    """Captures every enqueue call instead of touching Redis, so tests can
    assert exactly what the job engine scheduled without a live worker."""
    calls: list[tuple[str, int]] = []

    def fake_at(attempt_id, run_at):
        calls.append(("at", attempt_id))

    def fake_now(attempt_id):
        calls.append(("now", attempt_id))

    def fake_preflight(post_target_id, run_at):
        calls.append(("preflight", post_target_id))

    def fake_metrics_poll(post_target_id):
        calls.append(("metrics_poll", post_target_id))

    monkeypatch.setattr("jobs.queue.enqueue_attempt_at", fake_at)
    monkeypatch.setattr("jobs.queue.enqueue_attempt_now", fake_now)
    monkeypatch.setattr("jobs.queue.enqueue_preflight_at", fake_preflight)
    monkeypatch.setattr("jobs.queue.enqueue_metrics_poll", fake_metrics_poll)
    return calls


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Test Org")
    return Workspace.objects.create(organization=org, name="Test Workspace", slug="test-workspace")


@pytest.fixture
def mastodon_channel(db, workspace):
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="Mastodon")
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    return channel


@pytest.fixture
def post_target(db, workspace, mastodon_channel):
    post = Post.objects.create(workspace=workspace, kind="text", base_text="hello world")
    return PostTarget.objects.create(post=post, channel=mastodon_channel)
