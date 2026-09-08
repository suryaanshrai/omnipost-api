"""Media ingest and per-platform rendition pipeline.

Ingest (probe_asset) fills in the metadata MediaAsset previously never
computed — width/height/duration/mime were columns nothing wrote to, so
every capability check against them was silently a no-op. Renditions
(generate_rendition) produce a cropped/trimmed variant matching a target
shape (e.g. a 9:16 crop for Reels/Shorts, capped at 90s), cached by
(asset, profile) so the same source is never re-transcoded twice — see
MediaRendition.Meta.unique_together in omnipost_api/models.py.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import magic
from django.core.files.base import ContentFile
from omnipost_api.models import MediaAsset, MediaRendition
from PIL import Image

logger = logging.getLogger(__name__)

_FFPROBE_TIMEOUT_S = 60
_FFMPEG_TIMEOUT_S = 300
_MAX_RENDITION_DIMENSION = 2048


@dataclass(frozen=True)
class RenditionProfile:
    aspect: tuple[int, int]
    max_duration_s: float | None = None


# Named after the shape they produce, not any one platform — connectors
# reference these by name from their capability manifests as they land
# (Phase 6). Defined now so the pipeline and its tests don't have to wait.
RENDITION_PROFILES: dict[str, RenditionProfile] = {
    "square_1x1": RenditionProfile(aspect=(1, 1)),
    "portrait_4x5": RenditionProfile(aspect=(4, 5)),
    "vertical_9x16": RenditionProfile(aspect=(9, 16), max_duration_s=90),
    "landscape_16x9": RenditionProfile(aspect=(16, 9)),
}


def probe_asset(asset_id: int) -> None:
    """RQ job (media queue), enqueued right after upload."""
    try:
        asset = MediaAsset.objects.get(pk=asset_id)
    except MediaAsset.DoesNotExist:
        return

    with asset.file.open("rb") as fh:
        data = fh.read()
    asset.size_bytes = len(data)
    asset.mime_type = magic.from_buffer(data, mime=True)

    suffix = Path(asset.file.name).suffix or _suffix_for_mime(asset.mime_type)
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(data)
        # The handle above must be closed before ffprobe reads the file —
        # on Windows a still-open handle causes a sharing violation that
        # silently breaks the subprocess read.
        try:
            if asset.kind == MediaAsset.KIND_VIDEO:
                _probe_video(asset, tmp_path)
            else:
                _probe_image(asset, tmp_path)
        except Exception:
            logger.exception("Probe failed for MediaAsset %s", asset_id)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    asset.save(update_fields=["mime_type", "size_bytes", "width", "height", "duration_s"])


def _suffix_for_mime(mime_type: str) -> str:
    return "." + mime_type.split("/")[-1] if "/" in mime_type else ""


def _probe_image(asset: MediaAsset, path: str) -> None:
    with Image.open(path) as img:
        asset.width, asset.height = img.size


def _probe_video(asset: MediaAsset, path: str) -> None:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True,
        text=True,
        check=True,
        timeout=_FFPROBE_TIMEOUT_S,
    )
    data = json.loads(result.stdout)
    video_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video_stream:
        asset.width = video_stream.get("width")
        asset.height = video_stream.get("height")
    duration = data.get("format", {}).get("duration")
    if duration:
        asset.duration_s = float(duration)


def generate_rendition(asset_id: int, profile: str) -> int:
    """RQ job (media queue). Idempotent: reuses an existing (asset, profile)
    rendition instead of re-transcoding. Returns the MediaRendition id."""
    asset = MediaAsset.objects.get(pk=asset_id)
    existing = MediaRendition.objects.filter(asset=asset, profile=profile).first()
    if existing:
        return existing.pk

    spec = RENDITION_PROFILES[profile]
    with asset.file.open("rb") as fh:
        data = fh.read()

    src_suffix = Path(asset.file.name).suffix or ".bin"
    fd, src_path = tempfile.mkstemp(suffix=src_suffix)
    os.close(fd)
    out_path: str | None = None
    try:
        with open(src_path, "wb") as src_fh:
            src_fh.write(data)

        if asset.kind == MediaAsset.KIND_VIDEO:
            out_path, width, height, duration = _render_video(src_path, spec, asset.width, asset.height)
        else:
            # Animated GIFs are cropped on their first frame only — real
            # animated-GIF rendition support is out of scope for now.
            out_path, width, height = _render_image(src_path, spec)
            duration = None

        with open(out_path, "rb") as out_fh:
            rendition = MediaRendition(asset=asset, profile=profile, width=width, height=height, duration_s=duration)
            rendition.file.save(
                f"{asset.pk}_{profile}{Path(out_path).suffix}", ContentFile(out_fh.read()), save=False
            )
        rendition.save()
    finally:
        Path(src_path).unlink(missing_ok=True)
        if out_path:
            Path(out_path).unlink(missing_ok=True)

    return rendition.pk


def _target_crop(src_w: int, src_h: int, aspect: tuple[int, int]) -> tuple[int, int]:
    """Largest centered box matching `aspect` that fits inside src_w x src_h."""
    target_ratio = aspect[0] / aspect[1]
    if src_w / src_h > target_ratio:
        crop_h = src_h
        crop_w = int(round(crop_h * target_ratio))
    else:
        crop_w = src_w
        crop_h = int(round(crop_w / target_ratio))
    return crop_w, crop_h


def _render_image(path: str, spec: RenditionProfile) -> tuple[str, int, int]:
    with Image.open(path) as img:
        img = img.convert("RGB")
        crop_w, crop_h = _target_crop(img.width, img.height, spec.aspect)
        left = (img.width - crop_w) // 2
        top = (img.height - crop_h) // 2
        cropped = img.crop((left, top, left + crop_w, top + crop_h))
        if max(cropped.size) > _MAX_RENDITION_DIMENSION:
            scale = _MAX_RENDITION_DIMENSION / max(cropped.size)
            cropped = cropped.resize((max(1, int(cropped.width * scale)), max(1, int(cropped.height * scale))))
        fd, out_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        cropped.save(out_path, "JPEG", quality=90)
        return out_path, cropped.width, cropped.height


def _render_video(
    path: str, spec: RenditionProfile, src_w: int | None, src_h: int | None
) -> tuple[str, int, int, float]:
    if src_w is None or src_h is None:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path],
            capture_output=True,
            text=True,
            check=True,
            timeout=_FFPROBE_TIMEOUT_S,
        )
        stream = next(s for s in json.loads(probe.stdout)["streams"] if s.get("codec_type") == "video")
        src_w, src_h = int(stream["width"]), int(stream["height"])

    crop_w, crop_h = _target_crop(src_w, src_h, spec.aspect)
    fd, out_path = tempfile.mkstemp(suffix=".mp4")
    os.close(fd)

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        path,
        "-vf",
        f"crop={crop_w}:{crop_h}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
    ]
    if spec.max_duration_s:
        cmd += ["-t", str(spec.max_duration_s)]
    cmd.append(out_path)
    subprocess.run(cmd, capture_output=True, check=True, timeout=_FFMPEG_TIMEOUT_S)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", out_path],
        capture_output=True,
        text=True,
        check=True,
        timeout=_FFPROBE_TIMEOUT_S,
    )
    data = json.loads(probe.stdout)
    out_stream = next(s for s in data["streams"] if s.get("codec_type") == "video")
    duration = float(data["format"]["duration"])
    return out_path, out_stream["width"], out_stream["height"], duration
