from __future__ import annotations

from jobs.media import RENDITION_PROFILES
from rest_framework import serializers

from .models import Channel, MediaAsset, ProviderKey, VoiceProfile, Workspace

_PROVIDER_SLUGS = [choice[0] for choice in ProviderKey.PROVIDER_CHOICES]


class GenerateVariantsRequestSerializer(serializers.Serializer):
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.all())
    brief = serializers.CharField()
    channels = serializers.PrimaryKeyRelatedField(queryset=Channel.objects.all(), many=True)
    voice_profile = serializers.PrimaryKeyRelatedField(
        queryset=VoiceProfile.objects.all(), required=False, allow_null=True, default=None
    )
    provider = serializers.ChoiceField(choices=_PROVIDER_SLUGS, required=False, allow_null=True, default=None)


class RepurposeRequestSerializer(serializers.Serializer):
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.all())
    source_text = serializers.CharField(required=False, allow_blank=True, default="")
    source_url = serializers.URLField(required=False, allow_blank=True, default="")
    target_formats = serializers.ListField(child=serializers.CharField(), allow_empty=False)
    voice_profile = serializers.PrimaryKeyRelatedField(
        queryset=VoiceProfile.objects.all(), required=False, allow_null=True, default=None
    )
    provider = serializers.ChoiceField(choices=_PROVIDER_SLUGS, required=False, allow_null=True, default=None)

    def validate(self, attrs):
        if not attrs.get("source_text") and not attrs.get("source_url"):
            raise serializers.ValidationError("Provide either source_text or source_url.")
        return attrs


class AltTextRequestSerializer(serializers.Serializer):
    media = serializers.PrimaryKeyRelatedField(queryset=MediaAsset.objects.all())
    provider = serializers.ChoiceField(choices=_PROVIDER_SLUGS, required=False, allow_null=True, default=None)


class GenerateImageRequestSerializer(serializers.Serializer):
    workspace = serializers.PrimaryKeyRelatedField(queryset=Workspace.objects.all())
    prompt = serializers.CharField()
    aspect = serializers.ChoiceField(
        choices=list(RENDITION_PROFILES), required=False, allow_null=True, default=None
    )
    provider = serializers.ChoiceField(choices=_PROVIDER_SLUGS, required=False, allow_null=True, default=None)
