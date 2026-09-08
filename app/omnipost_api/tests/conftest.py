import pytest

from omnipost_api.models import Channel, Organization, User, Workspace


@pytest.fixture
def enqueued(monkeypatch):
    """Captures every enqueue call instead of touching Redis — see
    jobs/tests/conftest.py's identical fixture. Needed here too because
    several omnipost_api views (PostViewSet.schedule/queue/import_csv) call
    into jobs.scheduler.schedule_post_target."""
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
def user(db):
    return User.objects.create_user(username="alice", email="alice@example.com", password="x")


@pytest.fixture
def workspace(db, user):
    org = Organization.objects.create(name="Alice Org")
    return Workspace.objects.create(organization=org, name="Alice Workspace", slug="alice-workspace")


@pytest.fixture
def mastodon_channel(db, workspace):
    channel = Channel.objects.create(workspace=workspace, connector_slug="mastodon", display_name="My Mastodon")
    channel.set_credentials({"ACCESS_TOKEN": "tok", "INSTANCE_DOMAIN": "mastodon.social"})
    return channel
