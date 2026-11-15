from __future__ import annotations

from django.db import models as dj_models
from rest_framework import serializers
from rest_framework.validators import UniqueValidator

from .models import (
    AppCredential,
    BlackoutWindow,
    Channel,
    Feed,
    MediaAsset,
    Membership,
    Organization,
    Post,
    PostMetric,
    PostTarget,
    PostTargetPart,
    ProviderKey,
    PublishAttempt,
    QueueSlot,
    RecurrenceRule,
    User,
    VoiceProfile,
    Webhook,
    Workspace,
)


def _workspace_ids(context) -> list[int] | None:
    request = context.get("request")
    if request is None or not getattr(request.user, "is_authenticated", False):
        return None
    if "_workspace_ids" not in context:
        from .permissions import workspaces_for_user

        context["_workspace_ids"] = workspaces_for_user(request.user)
    return context["_workspace_ids"]


def _scope_related_field(field, workspace_ids: list[int]) -> None:
    """Narrow a related field's choices to rows in the user's workspaces, so
    an id from someone else's workspace is "does not exist", not accepted."""
    target = field.child_relation if isinstance(field, serializers.ManyRelatedField) else field
    if not isinstance(target, serializers.PrimaryKeyRelatedField) or target.queryset is None:
        return
    model = target.queryset.model
    field_names = {f.name for f in model._meta.get_fields()}
    if model is Workspace:
        target.queryset = target.queryset.filter(pk__in=workspace_ids)
    elif model is Organization:
        target.queryset = target.queryset.filter(workspaces__id__in=workspace_ids).distinct()
    elif "workspace" in field_names:
        target.queryset = target.queryset.filter(workspace_id__in=workspace_ids)
    elif "channel" in field_names:
        target.queryset = target.queryset.filter(channel__workspace_id__in=workspace_ids)
    elif "post" in field_names:
        target.queryset = target.queryset.filter(post__workspace_id__in=workspace_ids)


def _foreign_workspace_refs(value, workspace_id: int) -> bool:
    """True if any model instance inside `value` (nested dicts/lists of
    validated data) belongs to a workspace other than `workspace_id`."""
    if isinstance(value, dj_models.Model):
        if isinstance(value, Workspace):
            return False
        owner = getattr(value, "workspace_id", None)
        if owner is None and hasattr(value, "channel_id"):
            owner = getattr(value, "channel").workspace_id  # noqa: B009 - not every Model has .channel
        return owner is not None and owner != workspace_id
    if isinstance(value, dict):
        return any(_foreign_workspace_refs(v, workspace_id) for v in value.values())
    if isinstance(value, list | tuple):
        return any(_foreign_workspace_refs(v, workspace_id) for v in value)
    return False


def validate_timezone_name(value: str) -> str:
    """An IANA zone name, or blank. Bad names used to be stored and then
    crash best-times and queue-slot scheduling later."""
    if not value:
        return value
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise serializers.ValidationError(f"{value!r} isn't a time zone name like Europe/London.") from None
    return value


class WorkspaceScopedSerializer(serializers.ModelSerializer):
    """Base for every serializer whose rows belong to a workspace:
    * related-object ids are limited to the request user's workspaces;
    * every referenced object must belong to the row's own workspace (a post
      in workspace A can't target workspace B's channel, even when the user
      is a member of both);
    * `workspace` (and anything in `immutable_on_update`) can't be changed
      once the row exists."""

    immutable_on_update: tuple[str, ...] = ("workspace",)

    def get_fields(self):
        fields = super().get_fields()
        workspace_ids = _workspace_ids(self.context)
        if workspace_ids is not None:
            for field in fields.values():
                _scope_related_field(field, workspace_ids)
        if isinstance(self.instance, dj_models.Model):
            for name in self.immutable_on_update:
                if name in fields:
                    fields[name].read_only = True
        return fields

    def validate(self, attrs):
        attrs = super().validate(attrs)
        workspace = attrs.get("workspace") or getattr(self.instance, "workspace", None)
        if workspace is None and attrs.get("channel") is not None:
            workspace = attrs["channel"].workspace
        if workspace is not None and _foreign_workspace_refs(attrs, workspace.pk):
            raise serializers.ValidationError("Everything referenced must belong to the same workspace.")
        return attrs


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name"]
        read_only_fields = fields


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "created_at"]
        read_only_fields = ["id", "created_at"]


class WorkspaceSerializer(serializers.ModelSerializer):
    # Both optional on create: WorkspaceViewSet.perform_create() derives a
    # fresh Organization and a unique slug from `name` when either is
    # omitted, so a first-time user can self-serve "create my workspace"
    # with a name alone. Neither model field allows null/blank, so
    # ModelSerializer would otherwise mark both required — which, until
    # this override, silently defeated that fallback: a POST with only
    # `name` was rejected by validation before perform_create ever ran.
    # Always derived server-side: a new workspace gets its own Organization
    # (WorkspaceViewSet.perform_create). Accepting a client-supplied id let
    # anyone file a workspace under someone else's organization.
    organization = serializers.PrimaryKeyRelatedField(read_only=True)
    # Explicitly declaring this field (rather than letting ModelSerializer
    # auto-generate it) bypasses DRF's automatic UniqueValidator injection
    # for unique model fields, so it's added back by hand — a duplicate
    # slug should come back as a normal 400, not a database IntegrityError.
    slug = serializers.SlugField(
        max_length=220, required=False, validators=[UniqueValidator(queryset=Workspace.objects.all())]
    )

    class Meta:
        model = Workspace
        fields = [
            "id", "organization", "name", "slug", "timezone", "approval_workflow_enabled", "utm_tagging",
            "bio_enabled", "bio_description", "bio_links", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_timezone(self, value):
        return validate_timezone_name(value)

    def validate_bio_links(self, value):
        if not isinstance(value, list) or len(value) > 20:
            raise serializers.ValidationError("Up to 20 links.")
        clean = []
        for item in value:
            label = str((item or {}).get("label", "")).strip()[:80]
            url = str((item or {}).get("url", "")).strip()
            if not label or not url.startswith(("https://", "http://")):
                raise serializers.ValidationError("Each link needs a label and an http(s) URL.")
            clean.append({"label": label, "url": url[:500]})
        return clean


class MembershipSerializer(WorkspaceScopedSerializer):
    user = UserSerializer(read_only=True)
    # Write-only on create: who to add, by username or email. Only existing
    # accounts can be added; MembershipViewSet checks the caller is an admin.
    username = serializers.CharField(write_only=True, required=False)

    class Meta:
        model = Membership
        fields = ["id", "workspace", "user", "role", "created_at", "username"]
        read_only_fields = ["id", "created_at"]

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance is None:
            handle = (attrs.pop("username", "") or "").strip()
            if not handle:
                raise serializers.ValidationError({"username": "Give the username or email of the person to add."})
            user = User.objects.filter(dj_models.Q(username__iexact=handle) | dj_models.Q(email__iexact=handle)).first()
            if user is None:
                raise serializers.ValidationError({"username": "No OmniPost account with that username or email."})
            if Membership.objects.filter(workspace=attrs["workspace"], user=user).exists():
                raise serializers.ValidationError({"username": "That person is already a member."})
            attrs["user"] = user
        else:
            attrs.pop("username", None)
        return attrs


_DISCORD_WEBHOOK_HOSTS = {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}


class ChannelSerializer(WorkspaceScopedSerializer):
    immutable_on_update = ("workspace", "connector_slug")

    # Write-only: a dict of connector-specific credential fields (e.g.
    # {"ACCESS_TOKEN": "...", "IG_ID": "..."}). Never round-tripped back —
    # GET /channels/ never exposes secret values, matching each connector's
    # declared INSTANCE schema.
    credentials = serializers.DictField(child=serializers.CharField(), write_only=True, required=False)

    class Meta:
        model = Channel
        fields = [
            "id",
            "workspace",
            "connector_slug",
            "display_name",
            "external_account_id",
            "health",
            "health_detail",
            "token_expires_at",
            "refresh_token_expires_at",
            "timezone",
            "min_gap_minutes",
            "created_at",
            "credentials",
        ]
        read_only_fields = ["id", "health", "health_detail", "created_at"]

    def validate_timezone(self, value):
        return validate_timezone_name(value)

    def validate_connector_slug(self, value):
        from connectors import registry

        if value not in registry.all_slugs():
            raise serializers.ValidationError("Unknown platform.")
        return value

    def validate_credentials(self, value):
        """Credentials the server will later send requests to must point at
        the real platform, not at an address on the server's own network."""
        from connectors.netguard import UnsafeURL, assert_public_domain

        webhook = value.get("WEBHOOK_URL")
        if webhook is not None:
            from urllib.parse import urlsplit

            parts = urlsplit(webhook)
            if parts.scheme != "https" or parts.hostname not in _DISCORD_WEBHOOK_HOSTS:
                raise serializers.ValidationError({"WEBHOOK_URL": "Paste a Discord webhook URL (https://discord.com/…)."})
        domain = value.get("INSTANCE_DOMAIN")
        if domain is not None:
            try:
                assert_public_domain(domain)
            except UnsafeURL as exc:
                raise serializers.ValidationError({"INSTANCE_DOMAIN": str(exc)}) from exc
        return value

    def create(self, validated_data):
        credentials = validated_data.pop("credentials", None)
        channel = Channel.objects.create(**validated_data)
        if credentials:
            channel.set_credentials(credentials)
        return channel

    def update(self, instance, validated_data):
        """New credentials reconnect the channel in place: scheduled posts
        keep their target, and the health warning clears."""
        credentials = validated_data.pop("credentials", None)
        instance = super().update(instance, validated_data)
        if credentials:
            instance.set_credentials(credentials)
            instance.health = Channel.HEALTH_HEALTHY
            instance.health_detail = ""
            instance.save(update_fields=["health", "health_detail", "updated_at"])
        return instance


class MediaAssetSerializer(WorkspaceScopedSerializer):
    class Meta:
        model = MediaAsset
        fields = [
            "id",
            "workspace",
            "file",
            "kind",
            "mime_type",
            "size_bytes",
            "width",
            "height",
            "duration_s",
            "alt_text",
            "created_at",
        ]
        read_only_fields = ["id", "mime_type", "size_bytes", "width", "height", "duration_s", "created_at"]


class MediaPresignRequestSerializer(serializers.Serializer):
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.all())
    filename = serializers.CharField(max_length=255)
    content_type = serializers.CharField(max_length=255, default="application/octet-stream")


class MediaConfirmRequestSerializer(serializers.Serializer):
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.all())
    key = serializers.CharField(max_length=1024)
    kind = serializers.ChoiceField(choices=MediaAsset.KIND_CHOICES)
    alt_text = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")


class MediaRuleSerializer(serializers.Serializer):
    """Read-only view of connectors.capabilities.MediaRule — not a
    ModelSerializer, since Capabilities lives in code (connectors/*), not the
    database. Backs GET /connectors/, so the frontend's composer and Connect
    modal validate against the same rules Connector.validate() enforces
    server-side, instead of a hand-maintained table drifting out of sync."""

    kinds = serializers.ListField(child=serializers.CharField())
    max_count = serializers.IntegerField()
    max_size_mb = serializers.FloatField(allow_null=True)
    max_duration_s = serializers.FloatField(allow_null=True)
    min_duration_s = serializers.FloatField(allow_null=True)
    allowed_aspect_ratios = serializers.ListField(child=serializers.CharField())
    allowed_mime_types = serializers.ListField(child=serializers.CharField())


class ConnectorSerializer(serializers.Serializer):
    """Read-only view of one connectors.registry entry: its slug plus the
    capability/gating facts the frontend needs and can't otherwise discover
    (see GET /connectors/ in views.py)."""

    slug = serializers.CharField()
    display_name = serializers.CharField()
    requires_own_app = serializers.BooleanField()
    supports_oauth = serializers.BooleanField()
    # False when this deployment hasn't set up its own app for the platform
    # (e.g. no META_APP_ID): connecting would fail, so the UI says so up front.
    available = serializers.BooleanField()
    supports_metrics = serializers.BooleanField()
    supports_first_comment = serializers.BooleanField()
    credential_fields = serializers.ListField(child=serializers.CharField())
    oauth_extra_fields = serializers.ListField(child=serializers.CharField())
    max_text_length = serializers.IntegerField(allow_null=True)
    supports_link = serializers.BooleanField()
    supports_alt_text = serializers.BooleanField()
    supports_threads = serializers.BooleanField()
    supports_scheduling_native = serializers.BooleanField()
    post_kinds = serializers.ListField(child=serializers.CharField())
    media = MediaRuleSerializer(many=True)


class ValidateFindingSerializer(serializers.Serializer):
    """One connectors.errors.Finding — shared by POST /validate/ and the AI
    variant generator, which both report per-channel rule findings."""

    code = serializers.CharField()
    message = serializers.CharField()
    blocking = serializers.BooleanField()
    field = serializers.CharField(allow_null=True)


class BestTimeSerializer(serializers.Serializer):
    """Read-only shape of one services.best_times.Suggestion — backs
    GET /channels/{id}/best-times/."""

    weekday = serializers.IntegerField()
    hour = serializers.IntegerField()
    sample_size = serializers.IntegerField()
    avg_engagement = serializers.FloatField()


class ValidateRequestSerializer(serializers.Serializer):
    channel = serializers.PrimaryKeyRelatedField(queryset=Channel.objects.all())
    text = serializers.CharField(allow_blank=True, default="")
    post_kind = serializers.CharField(default="text")
    link = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)
    media = serializers.PrimaryKeyRelatedField(
        queryset=MediaAsset.objects.all(), many=True, required=False, default=list
    )


class PostTargetPartWriteSerializer(WorkspaceScopedSerializer):
    class Meta:
        model = PostTargetPart
        fields = ["sequence", "text", "media", "delay_after_s"]


class PostTargetPartSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostTargetPart
        fields = ["id", "sequence", "text", "media", "delay_after_s"]
        read_only_fields = ["id"]



class PostTargetWriteSerializer(WorkspaceScopedSerializer):
    # Write-only: a thread/drip's ordered posts. Only meaningful for a
    # connector whose capabilities declare supports_threads — see
    # connectors/base.py::Connector.validate().
    parts = PostTargetPartWriteSerializer(many=True, write_only=True, required=False)

    class Meta:
        model = PostTarget
        fields = ["channel", "format", "text_override", "first_comment", "media", "parts"]


class PostTargetSerializer(serializers.ModelSerializer):
    parts = PostTargetPartSerializer(many=True, read_only=True)

    class Meta:
        model = PostTarget
        fields = [
            "id",
            "post",
            "channel",
            "format",
            "text_override",
            "first_comment",
            "first_comment_result",
            "media",
            "parts",
            "status",
            "run_at",
            "remote_id",
            "permalink",
            "published_at",
            "epoch",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "first_comment_result",
            "status",
            "remote_id",
            "permalink",
            "published_at",
            "epoch",
            "created_at",
            "updated_at",
        ]


class CalendarEntrySerializer(PostTargetSerializer):
    """One PostTarget as the calendar draws it: the target plus just enough
    of its parent post (kind, text, status) to label the entry, so the
    calendar doesn't need a second request per post to say what's going out."""

    post_kind = serializers.CharField(source="post.kind", read_only=True)
    post_text = serializers.CharField(source="post.base_text", read_only=True)
    post_status = serializers.CharField(source="post.status", read_only=True)

    class Meta(PostTargetSerializer.Meta):
        fields = [*PostTargetSerializer.Meta.fields, "post_kind", "post_text", "post_status"]


class PostMetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostMetric
        fields = ["id", "post_target", "impressions", "likes", "comments", "shares", "raw", "fetched_at"]
        read_only_fields = fields


class PostSerializer(WorkspaceScopedSerializer):
    targets = PostTargetSerializer(many=True, read_only=True)
    # Write-only: creates PostTarget rows alongside the Post in one call.
    target_specs = PostTargetWriteSerializer(many=True, write_only=True, required=False)

    class Meta:
        model = Post
        fields = [
            "id",
            "workspace",
            "author",
            "kind",
            "base_text",
            "base_media",
            "link",
            "status",
            "scheduled_for",
            "ai_generated",
            "voice_profile",
            "created_at",
            "updated_at",
            "targets",
            "target_specs",
        ]
        read_only_fields = ["id", "status", "author", "created_at", "updated_at"]

    def create(self, validated_data):
        target_specs = validated_data.pop("target_specs", [])
        base_media = validated_data.pop("base_media", [])
        post = Post.objects.create(**validated_data)
        if base_media:
            post.base_media.set(base_media)
        for spec in target_specs:
            self._create_target(post, spec)
        return post

    def update(self, instance, validated_data):
        # target_specs is write-only and, until now, create-only: a draft's
        # channel targets couldn't be changed after the Post was made, so a
        # composer/drafts screen had nowhere to send "actually, also post
        # this to LinkedIn". Omitting target_specs from the PATCH body leaves
        # existing targets untouched, same as any other field; passing it
        # replaces the full set (mirrors create()'s all-or-nothing shape).
        target_specs = validated_data.pop("target_specs", None)
        base_media = validated_data.pop("base_media", None)
        if target_specs is not None and instance.status not in (Post.STATUS_DRAFT, Post.STATUS_FAILED):
            # Targets carry cascade-deleted PublishAttempt/PostMetric history
            # once a post has actually gone out — replacing them on anything
            # past draft/failed would silently destroy that record.
            raise serializers.ValidationError(
                {"target_specs": f"Cannot change targets on a post in status '{instance.status}'."}
            )
        instance = super().update(instance, validated_data)
        if base_media is not None:
            instance.base_media.set(base_media)
        if target_specs is not None:
            instance.targets.all().delete()
            for spec in target_specs:
                self._create_target(instance, spec)
        return instance

    @staticmethod
    def _create_target(post, spec):
        spec = dict(spec)
        media = spec.pop("media", [])
        parts = spec.pop("parts", [])
        target = PostTarget.objects.create(post=post, **spec)
        if media:
            target.media.set(media)
        for part_spec in parts:
            part_spec = dict(part_spec)
            part_media = part_spec.pop("media", [])
            part = PostTargetPart.objects.create(post_target=target, **part_spec)
            if part_media:
                part.media.set(part_media)
        return target


class PublishAttemptSerializer(serializers.ModelSerializer):
    class Meta:
        model = PublishAttempt
        fields = [
            "id",
            "post_target",
            "epoch",
            "attempt_number",
            "status",
            "error_class",
            "error_detail",
            "state_step",
            "run_at",
            "started_at",
            "finished_at",
            "created_at",
        ]
        read_only_fields = fields


class VoiceProfileSerializer(WorkspaceScopedSerializer):
    class Meta:
        model = VoiceProfile
        fields = ["id", "workspace", "name", "style_summary", "example_posts", "is_default", "updated_at"]
        read_only_fields = ["id", "updated_at"]


class ProviderKeySerializer(WorkspaceScopedSerializer):
    api_key = serializers.CharField(write_only=True)

    class Meta:
        model = ProviderKey
        fields = ["id", "workspace", "provider", "label", "is_active", "last_validated_at", "created_at", "api_key"]
        read_only_fields = ["id", "last_validated_at", "created_at"]

    def create(self, validated_data):
        from ai import providers as ai_providers
        from ai.errors import AIProviderError
        from django.conf import settings
        from django.utils import timezone

        from . import crypto

        api_key = validated_data.pop("api_key")
        # Validated with a minimal live request before it's ever stored, so a
        # bad key is rejected here instead of surfacing as a mysterious
        # failure on the workspace's first real generation.
        try:
            ai_providers.ping(validated_data["provider"], api_key, settings.AI_TEXT_MODELS[validated_data["provider"]])
        except AIProviderError as exc:
            raise serializers.ValidationError({"api_key": f"Could not validate this key: {exc.safe_detail}"}) from exc

        key = ProviderKey.objects.create(**validated_data, encrypted_key={}, last_validated_at=timezone.now())
        key.encrypted_key = crypto.encrypt(api_key, aad=key._aad())
        key.save(update_fields=["encrypted_key"])
        return key

    def update(self, instance, validated_data):
        """A new api_key is validated and re-encrypted; before this, a PATCH
        with a new key returned 200 and silently kept the old one."""
        from ai import providers as ai_providers
        from ai.errors import AIProviderError
        from django.conf import settings
        from django.utils import timezone

        from . import crypto

        api_key = validated_data.pop("api_key", None)
        instance = super().update(instance, validated_data)
        if api_key:
            try:
                ai_providers.ping(instance.provider, api_key, settings.AI_TEXT_MODELS[instance.provider])
            except AIProviderError as exc:
                detail = f"Could not validate this key: {exc.safe_detail}"
                raise serializers.ValidationError({"api_key": detail}) from exc
            instance.encrypted_key = crypto.encrypt(api_key, aad=instance._aad())
            instance.last_validated_at = timezone.now()
            instance.save(update_fields=["encrypted_key", "last_validated_at"])
        return instance


class AppCredentialSerializer(WorkspaceScopedSerializer):
    """client_secret is write-only and never echoed back — unlike
    ProviderKeySerializer there's no live ping to validate it against here,
    since exercising it means actually starting an OAuth flow with the
    platform (see PublishView-adjacent oauth.py); a typo surfaces the first
    time the workspace tries to connect a channel."""

    client_secret = serializers.CharField(write_only=True)

    class Meta:
        model = AppCredential
        fields = ["id", "workspace", "connector_slug", "client_id", "label", "created_at", "client_secret"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        from . import crypto

        client_secret = validated_data.pop("client_secret")
        credential = AppCredential.objects.create(**validated_data, encrypted_client_secret={})
        credential.encrypted_client_secret = crypto.encrypt(client_secret, aad=credential._aad())
        credential.save(update_fields=["encrypted_client_secret"])
        return credential

    def update(self, instance, validated_data):
        from . import crypto

        client_secret = validated_data.pop("client_secret", None)
        instance = super().update(instance, validated_data)
        if client_secret is not None:
            instance.encrypted_client_secret = crypto.encrypt(client_secret, aad=instance._aad())
            instance.save(update_fields=["encrypted_client_secret"])
        return instance


class QueueSlotSerializer(WorkspaceScopedSerializer):
    immutable_on_update = ("channel",)

    class Meta:
        model = QueueSlot
        fields = ["id", "channel", "weekday", "time_of_day", "is_active"]
        read_only_fields = ["id"]

    def validate_weekday(self, value):
        if not 0 <= value <= 6:
            raise serializers.ValidationError("weekday is 0 (Monday) to 6 (Sunday).")
        return value


class BlackoutWindowSerializer(WorkspaceScopedSerializer):
    class Meta:
        model = BlackoutWindow
        fields = ["id", "workspace", "channel", "label", "starts_at", "ends_at", "is_active"]
        read_only_fields = ["id"]

    def validate(self, attrs):
        starts_at = attrs.get("starts_at", getattr(self.instance, "starts_at", None))
        ends_at = attrs.get("ends_at", getattr(self.instance, "ends_at", None))
        if starts_at and ends_at and starts_at >= ends_at:
            raise serializers.ValidationError("starts_at must be before ends_at.")
        return attrs


class RecurrenceRuleSerializer(WorkspaceScopedSerializer):
    class Meta:
        model = RecurrenceRule
        fields = [
            "id",
            "workspace",
            "channel",
            "kind",
            "variants",
            "link",
            "media",
            "interval_hours",
            "next_variant_index",
            "next_run_at",
            "end_at",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "next_variant_index", "created_at", "updated_at"]

    def validate_variants(self, value):
        if not value or not any(str(v).strip() for v in value):
            raise serializers.ValidationError("Provide at least one non-empty text variant.")
        return value

    def validate_interval_hours(self, value):
        if value < 1:
            raise serializers.ValidationError("interval_hours must be at least 1.")
        return value


class WebhookSerializer(WorkspaceScopedSerializer):
    # Generated server-side and returned only in the create response.
    secret = serializers.CharField(read_only=True)
    events = serializers.ListField(
        child=serializers.ChoiceField(choices=Webhook.EVENTS), required=False, allow_empty=True
    )

    class Meta:
        model = Webhook
        fields = [
            "id", "workspace", "url", "events", "is_active", "last_status", "last_delivered_at", "created_at", "secret",
        ]
        read_only_fields = ["id", "last_status", "last_delivered_at", "created_at"]

    def validate_url(self, value):
        from connectors.netguard import UnsafeURL, assert_public_url

        try:
            assert_public_url(value, schemes=("https",))
        except UnsafeURL as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self.context.get("show_secret"):
            data.pop("secret", None)
        return data

    def create(self, validated_data):
        import secrets

        validated_data["secret"] = secrets.token_hex(32)
        return super().create(validated_data)


class FeedSerializer(WorkspaceScopedSerializer):
    class Meta:
        model = Feed
        fields = [
            "id", "workspace", "url", "channels", "template", "is_active",
            "last_checked_at", "last_error", "created_at",
        ]
        read_only_fields = ["id", "last_checked_at", "last_error", "created_at"]

    def validate_url(self, value):
        from connectors.netguard import UnsafeURL, assert_public_url

        try:
            assert_public_url(value)
        except UnsafeURL as exc:
            raise serializers.ValidationError(str(exc)) from exc
        return value
