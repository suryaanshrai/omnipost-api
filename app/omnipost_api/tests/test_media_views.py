import io

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image
from rest_framework.test import APIClient

from omnipost_api.models import MediaAsset


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _jpeg_upload(name="photo.jpg") -> SimpleUploadedFile:
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), color="red").save(buf, "JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")


def test_multipart_upload_creates_asset_and_enqueues_probe(db, django_user_model, monkeypatch):
    user = django_user_model.objects.create_user(username="uma", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    enqueued = []
    monkeypatch.setattr("jobs.queue.enqueue_probe", lambda asset_id: enqueued.append(asset_id))

    response = client.post(
        "/media/",
        {"workspace": workspace.pk, "kind": "image", "file": _jpeg_upload()},
        format="multipart",
    )
    assert response.status_code == 201, response.data
    asset = MediaAsset.objects.get(pk=response.data["id"])
    assert asset.workspace_id == workspace.pk
    assert asset.uploaded_by_id == user.pk
    assert enqueued == [asset.pk]


def test_upload_rejects_workspace_the_user_cannot_access(db, django_user_model):
    user = django_user_model.objects.create_user(username="vic", password="x")
    other_user = django_user_model.objects.create_user(username="wren", password="x")
    other_workspace = other_user.memberships.get().workspace
    client = _client_for(user)

    response = client.post(
        "/media/",
        {"workspace": other_workspace.pk, "kind": "image", "file": _jpeg_upload()},
        format="multipart",
    )
    assert response.status_code == 403


def test_presign_returns_501_when_s3_not_configured(db, django_user_model):
    user = django_user_model.objects.create_user(username="xander", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    response = client.post(
        "/media/presign/",
        {"workspace": workspace.pk, "filename": "clip.mp4", "content_type": "video/mp4"},
        format="json",
    )
    assert response.status_code == 501


def test_presign_returns_upload_fields_when_s3_enabled(db, django_user_model, monkeypatch):
    from omnipost_api import views as views_module

    user = django_user_model.objects.create_user(username="yara", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    monkeypatch.setattr(views_module.settings, "S3_MEDIA_ENABLED", True)
    monkeypatch.setattr(views_module.settings, "AWS_STORAGE_BUCKET_NAME", "test-bucket")

    class FakeClient:
        def generate_presigned_post(self, **kwargs):
            return {"url": "https://test-bucket.s3.amazonaws.com/", "fields": {"key": kwargs["Key"]}}

    class FakeMeta:
        client = FakeClient()

    class FakeConnection:
        meta = FakeMeta()

    class FakeStorage:
        connection = FakeConnection()

    monkeypatch.setattr(views_module, "default_storage", FakeStorage())

    response = client.post(
        "/media/presign/",
        {"workspace": workspace.pk, "filename": "clip.mp4", "content_type": "video/mp4"},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["key"].startswith("media/originals/")
    assert response.data["key"].endswith("clip.mp4")
    assert response.data["upload"]["fields"]["key"] == response.data["key"]


def test_confirm_from_key_requires_s3_enabled(db, django_user_model):
    user = django_user_model.objects.create_user(username="zane", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    response = client.post(
        "/media/",
        {"workspace": workspace.pk, "kind": "video", "key": "media/originals/abc/clip.mp4"},
        format="json",
    )
    assert response.status_code == 400


def test_confirm_from_key_creates_asset_pointing_at_existing_object(db, django_user_model, monkeypatch):
    from omnipost_api import views as views_module

    user = django_user_model.objects.create_user(username="asha", password="x")
    workspace = user.memberships.get().workspace
    client = _client_for(user)

    monkeypatch.setattr(views_module.settings, "S3_MEDIA_ENABLED", True)
    enqueued = []
    monkeypatch.setattr("jobs.queue.enqueue_probe", lambda asset_id: enqueued.append(asset_id))

    response = client.post(
        "/media/",
        {"workspace": workspace.pk, "kind": "video", "key": "media/originals/abc/clip.mp4"},
        format="json",
    )
    assert response.status_code == 201, response.data
    asset = MediaAsset.objects.get(pk=response.data["id"])
    assert asset.file.name == "media/originals/abc/clip.mp4"
    assert enqueued == [asset.pk]
