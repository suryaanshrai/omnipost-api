"""Real ffprobe/ffmpeg/Pillow exercise of the media pipeline — deliberately
not mocked, since the entire point of this pipeline is that the ffprobe JSON
shape and ffmpeg crop filter actually work, not that our code calls a
subprocess. Both binaries are required in dev/CI/Docker (see Dockerfile)."""

from __future__ import annotations

import io
import subprocess

import pytest
from django.core.files.base import ContentFile
from omnipost_api.models import MediaAsset, MediaRendition, Organization, Workspace
from PIL import Image

from jobs.media import RenditionProfile, generate_rendition, probe_asset


@pytest.fixture
def workspace(db):
    org = Organization.objects.create(name="Media Test Org")
    return Workspace.objects.create(organization=org, name="Media Test WS", slug="media-test-ws")


def _jpeg_bytes(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(200, 50, 50)).save(buf, "JPEG")
    return buf.getvalue()


def _mp4_bytes(width: int, height: int, duration_s: float) -> bytes:
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:duration={duration_s}:rate=10",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=60,
    )
    return result.stdout


@pytest.fixture
def image_asset(db, workspace):
    asset = MediaAsset(workspace=workspace, kind=MediaAsset.KIND_IMAGE)
    asset.file.save("original.jpg", ContentFile(_jpeg_bytes(800, 600)), save=False)
    asset.save()
    return asset


@pytest.fixture
def video_asset(db, workspace):
    asset = MediaAsset(workspace=workspace, kind=MediaAsset.KIND_VIDEO)
    asset.file.save("original.mp4", ContentFile(_mp4_bytes(640, 480, 2)), save=False)
    asset.save()
    return asset


def test_probe_asset_fills_in_image_metadata(image_asset):
    probe_asset(image_asset.pk)
    image_asset.refresh_from_db()
    assert image_asset.width == 800
    assert image_asset.height == 600
    assert image_asset.mime_type == "image/jpeg"
    assert image_asset.size_bytes and image_asset.size_bytes > 0


def test_probe_asset_fills_in_video_metadata(video_asset):
    probe_asset(video_asset.pk)
    video_asset.refresh_from_db()
    assert video_asset.width == 640
    assert video_asset.height == 480
    assert video_asset.duration_s is not None
    assert 1.5 < video_asset.duration_s < 2.5
    assert video_asset.size_bytes and video_asset.size_bytes > 0


def test_probe_asset_missing_row_is_a_noop(db):
    probe_asset(999999)  # must not raise


def test_generate_rendition_crops_image_to_target_aspect(image_asset):
    rendition_id = generate_rendition(image_asset.pk, "portrait_4x5")
    rendition = MediaRendition.objects.get(pk=rendition_id)
    assert rendition.profile == "portrait_4x5"
    assert rendition.width and rendition.height
    ratio = rendition.width / rendition.height
    assert abs(ratio - (4 / 5)) < 0.02


def test_generate_rendition_is_idempotent(image_asset):
    first_id = generate_rendition(image_asset.pk, "square_1x1")
    second_id = generate_rendition(image_asset.pk, "square_1x1")
    assert first_id == second_id
    assert MediaRendition.objects.filter(asset=image_asset, profile="square_1x1").count() == 1


def test_generate_rendition_crops_video_to_target_aspect(video_asset):
    rendition_id = generate_rendition(video_asset.pk, "vertical_9x16")
    rendition = MediaRendition.objects.get(pk=rendition_id)
    assert rendition.width and rendition.height
    ratio = rendition.width / rendition.height
    assert abs(ratio - (9 / 16)) < 0.02
    assert rendition.duration_s is not None


def test_generate_rendition_trims_video_to_max_duration(video_asset, monkeypatch):
    import jobs.media as media_module

    monkeypatch.setitem(
        media_module.RENDITION_PROFILES, "trim_test", RenditionProfile(aspect=(1, 1), max_duration_s=1)
    )
    rendition_id = generate_rendition(video_asset.pk, "trim_test")
    rendition = MediaRendition.objects.get(pk=rendition_id)
    assert rendition.duration_s is not None
    assert rendition.duration_s <= 1.5
