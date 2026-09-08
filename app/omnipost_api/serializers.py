from __future__ import annotations

from rest_framework import serializers

from .models import (
    AppCredential,
    BlackoutWindow,
    Channel,
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
    Workspace,
)


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "first_name", "last_name"]
        read_only_fields = fields


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "plan", "created_at"]
        read_only_fields = ["id", "created_at"]


class WorkspaceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Workspace
        fields = ["id", "organization", "name", "slug", "timezone", "approval_workflow_enabled", "created_at"]
        read_only_fields = ["id", "created_at"]


class MembershipSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "workspace", "user", "role", "created_at"]
        read_only_fields = ["id", "created_at"]


class ChannelSerializer(serializers.ModelSerializer):
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

    def create(self, validated_data):
        credentials = validated_data.pop("credentials", None)
        channel = Channel.objects.create(**validated_data)
        if credentials:
            channel.set_credentials(credentials)
        return channel

    def update(self, instance, validated_data):
        credentials = validated_data.pop("credentials", None)
        instance = super().update(instance, validated_data)
        if credentials:
            instance.set_credentials(credentials)
        return instance


class MediaAssetSerializer(serializers.ModelSerializer):
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


class ValidateRequestSerializer(serializers.Serializer):
    channel = serializers.PrimaryKeyRelatedField(queryset=Channel.objects.all())
    text = serializers.CharField(allow_blank=True, default="")
    post_kind = serializers.CharField(default="text")
    link = serializers.CharField(required=False, allow_blank=True, allow_null=True, default=None)
    media = serializers.PrimaryKeyRelatedField(
        queryset=MediaAsset.objects.all(), many=True, required=False, default=list
    )


class PostTargetPartWriteSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostTargetPart
        fields = ["sequence", "text", "media", "delay_after_s"]


class PostTargetPartSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostTargetPart
        fields = ["id", "sequence", "text", "media", "delay_after_s"]
        read_only_fields = ["id"]


class PostTargetWriteSerializer(serializers.ModelSerializer):
    # Write-only: a thread/drip's ordered posts. Only meaningful for a
    # connector whose capabilities declare supports_threads — see
    # connectors/base.py::Connector.validate().
    parts = PostTargetPartWriteSerializer(many=True, write_only=True, required=False)

    class Meta:
        model = PostTarget
        fields = ["channel", "format", "text_override", "media", "parts"]


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
            "status",
            "remote_id",
            "permalink",
            "published_at",
            "epoch",
            "created_at",
            "updated_at",
        ]


class PostMetricSerializer(serializers.ModelSerializer):
    class Meta:
        model = PostMetric
        fields = ["id", "post_target", "impressions", "likes", "comments", "shares", "raw", "fetched_at"]
        read_only_fields = fields


class PostSerializer(serializers.ModelSerializer):
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
            media = spec.pop("media", [])
            parts = spec.pop("parts", [])
            target = PostTarget.objects.create(post=post, **spec)
            if media:
                target.media.set(media)
            for part_spec in parts:
                part_media = part_spec.pop("media", [])
                part = PostTargetPart.objects.create(post_target=target, **part_spec)
                if part_media:
                    part.media.set(part_media)
        return post


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


class VoiceProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = VoiceProfile
        fields = ["id", "workspace", "name", "style_summary", "example_posts", "is_default", "updated_at"]
        read_only_fields = ["id", "updated_at"]


class ProviderKeySerializer(serializers.ModelSerializer):
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


class AppCredentialSerializer(serializers.ModelSerializer):
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


class QueueSlotSerializer(serializers.ModelSerializer):
    class Meta:
        model = QueueSlot
        fields = ["id", "channel", "weekday", "time_of_day", "is_active"]
        read_only_fields = ["id"]


class BlackoutWindowSerializer(serializers.ModelSerializer):
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


class RecurrenceRuleSerializer(serializers.ModelSerializer):
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
