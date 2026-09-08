import json

import pytest
from ai import service
from ai.errors import AIProviderError
from django.core.files.base import ContentFile
from omnipost_api.models import MediaAsset


def test_generate_variants_returns_clean_text(mastodon_channel, monkeypatch):
    monkeypatch.setattr(
        "ai.gateway.run_text", lambda workspace, **kw: json.dumps({str(mastodon_channel.pk): "short and sweet"})
    )
    results = service.generate_variants(
        mastodon_channel.workspace,
        brief="announce our launch",
        channels=[mastodon_channel],
        voice_profile=None,
        provider=None,
    )
    assert results == [{"channel": mastodon_channel.pk, "text": "short and sweet", "findings": []}]


def test_generate_variants_truncates_over_limit_text_and_still_passes_preflight(mastodon_channel, monkeypatch):
    too_long = "x" * 600  # mastodon's spec caps at 500
    monkeypatch.setattr("ai.gateway.run_text", lambda workspace, **kw: json.dumps({str(mastodon_channel.pk): too_long}))

    results = service.generate_variants(
        mastodon_channel.workspace, brief="brief", channels=[mastodon_channel], voice_profile=None, provider=None
    )
    variant = results[0]
    assert len(variant["text"]) <= 500
    assert variant["text"].endswith("…")
    assert variant["findings"] == []  # the guarantee: preflight passes after truncation


def test_generate_variants_missing_channel_in_model_output_yields_empty_text(mastodon_channel, monkeypatch):
    monkeypatch.setattr("ai.gateway.run_text", lambda workspace, **kw: json.dumps({}))
    results = service.generate_variants(
        mastodon_channel.workspace, brief="brief", channels=[mastodon_channel], voice_profile=None, provider=None
    )
    assert results[0]["text"] == ""


def test_repurpose_content_with_source_text(workspace, monkeypatch):
    monkeypatch.setattr(
        "ai.gateway.run_text", lambda ws, **kw: json.dumps({"thread": "a thread", "linkedin_post": "a post"})
    )
    result = service.repurpose_content(
        workspace, source_text="my blog post body", source_url=None, target_formats=["thread", "linkedin_post"],
        voice_profile=None, provider=None,
    )
    assert result == {"thread": "a thread", "linkedin_post": "a post"}


def test_repurpose_content_fetches_source_url(workspace, monkeypatch):
    monkeypatch.setattr(service, "fetch_source_text", lambda url: "fetched article text")
    captured = {}

    def fake_run_text(ws, **kw):
        captured["prompt"] = kw["prompt"]
        return json.dumps({"thread": "ok"})

    monkeypatch.setattr("ai.gateway.run_text", fake_run_text)
    result = service.repurpose_content(
        workspace, source_text=None, source_url="https://example.com/post", target_formats=["thread"],
        voice_profile=None, provider=None,
    )
    assert result == {"thread": "ok"}
    assert "fetched article text" in captured["prompt"]


def test_repurpose_content_requires_some_source(workspace):
    with pytest.raises(AIProviderError):
        service.repurpose_content(
            workspace, source_text=None, source_url=None, target_formats=["thread"], voice_profile=None, provider=None
        )


def test_extract_text_from_html_strips_tags_and_scripts():
    markup = (
        "<html><head><style>.x{}</style></head><body>"
        "<script>evil()</script><p>Hello &amp; welcome</p></body></html>"
    )
    text = service._extract_text_from_html(markup)
    assert "evil()" not in text
    assert ".x{}" not in text
    assert "Hello & welcome" in text


def _image_asset(workspace, *, mime_type="image/png"):
    asset = MediaAsset(workspace=workspace, kind=MediaAsset.KIND_IMAGE, mime_type=mime_type)
    asset.file.save("test.png", ContentFile(b"fake-png-bytes"), save=False)
    asset.save()
    return asset


def test_generate_alt_text_updates_asset(workspace, monkeypatch):
    asset = _image_asset(workspace)
    monkeypatch.setattr("ai.gateway.run_text", lambda ws, **kw: "a red circle on white background")

    result = service.generate_alt_text(workspace, asset, provider=None)
    assert result == "a red circle on white background"
    asset.refresh_from_db()
    assert asset.alt_text == "a red circle on white background"


def test_generate_alt_text_passes_image_to_gateway(workspace, monkeypatch):
    asset = _image_asset(workspace)
    captured = {}

    def fake_run_text(ws, **kw):
        captured["image"] = kw.get("image")
        return "alt"

    monkeypatch.setattr("ai.gateway.run_text", fake_run_text)
    service.generate_alt_text(workspace, asset, provider=None)
    assert captured["image"] is not None
    assert captured["image"].media_type == "image/png"


def test_generate_alt_text_rejects_video(workspace):
    asset = MediaAsset(workspace=workspace, kind=MediaAsset.KIND_VIDEO, mime_type="video/mp4")
    asset.file.save("test.mp4", ContentFile(b"fake"), save=False)
    asset.save()
    with pytest.raises(AIProviderError):
        service.generate_alt_text(workspace, asset, provider=None)


def test_generate_alt_text_rejects_unprocessed_media(workspace):
    asset = MediaAsset(workspace=workspace, kind=MediaAsset.KIND_IMAGE, mime_type="")
    asset.file.save("test.png", ContentFile(b"fake"), save=False)
    asset.save()
    with pytest.raises(AIProviderError):
        service.generate_alt_text(workspace, asset, provider=None)


def test_generate_image_asset_creates_media_asset_and_enqueues_probe(workspace, monkeypatch):
    monkeypatch.setattr("ai.gateway.run_image", lambda ws, **kw: b"fake-png-bytes")
    enqueued = []
    monkeypatch.setattr("jobs.queue.enqueue_probe", lambda asset_id: enqueued.append(asset_id))

    asset = service.generate_image_asset(
        workspace, prompt="a cat", aspect="square_1x1", uploaded_by=None, provider=None
    )
    assert asset.pk is not None
    assert asset.kind == MediaAsset.KIND_IMAGE
    assert enqueued == [asset.pk]


def test_generate_image_asset_rejects_unknown_aspect(workspace):
    with pytest.raises(AIProviderError):
        service.generate_image_asset(
            workspace, prompt="a cat", aspect="not-a-real-profile", uploaded_by=None, provider=None
        )
