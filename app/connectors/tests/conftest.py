import pytest


@pytest.fixture
def meta_app_env(monkeypatch):
    monkeypatch.setenv("META_APP_ID", "meta-app-id")
    monkeypatch.setenv("META_APP_SECRET", "meta-app-secret")


@pytest.fixture
def threads_app_env(monkeypatch):
    monkeypatch.setenv("THREADS_APP_ID", "threads-app-id")
    monkeypatch.setenv("THREADS_APP_SECRET", "threads-app-secret")


@pytest.fixture
def tiktok_app_env(monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "tiktok-client-key")
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "tiktok-client-secret")


@pytest.fixture
def youtube_app_env(monkeypatch):
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "youtube-client-id")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "youtube-client-secret")
