# serializers.py
from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    Platform, PlatformInstance, PostText, PostImage, PostVideo,
    ShortFormVideo, Stories, Doc, Notification
)
from django.core.exceptions import ValidationError as DjangoValidationError
# Assuming FernetEncryptor and zxcvbn might be needed for validation if uncommented in model
# from .models import FernetEncryptor # Assuming FernetEncryptor is in models.py or accessible
# from zxcvbn import zxcvbn

User = get_user_model()

class UserSerializer(serializers.ModelSerializer):
    """
    Serializer for the User model.
    """
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name'] # Add other fields as needed, exclude password
        read_only_fields = ['id', 'email'] # Typically email is set during signup

class PlatformSerializer(serializers.ModelSerializer):
    """
    Serializer for the Platform model.
    Includes configuration details.
    """
    class Meta:
        model = Platform
        fields = ['id', 'name', 'config']
        read_only_fields = ['id']

class PlatformInstanceSerializer(serializers.ModelSerializer):
    """
    Serializer for the PlatformInstance model.
    Handles credentials securely.
    Includes a write-only password field for encryption during save.
    """
    # Include platform details when reading
    platform = PlatformSerializer(read_only=True)
    # Use PrimaryKeyRelatedField for writing to associate with an existing Platform
    platform_id = serializers.PrimaryKeyRelatedField(
        queryset=Platform.objects.all(), source='platform', write_only=True
    )
    # Use PrimaryKeyRelatedField for user association (read-only, set by view)
    user = serializers.PrimaryKeyRelatedField(read_only=True)
    # Make credentials write-only for security. They shouldn't be exposed directly.
    credentials = serializers.JSONField(write_only=True, required=True)
    # Add a write-only password field to pass for potential encryption in the model's save
    # password = serializers.CharField(write_only=True, required=False, allow_null=True, style={'input_type': 'password'})

    class Meta:
        model = PlatformInstance
        fields = ['id', 'platform', 'platform_id', 'user', 'credentials'] # Removed 'password' field as model logic is commented
        read_only_fields = ['id', 'user'] # User is set automatically by the view

    # If the encryption logic in PlatformInstance.save were active,
    # you might add validation for the password here or in the view.
    # def validate_password(self, value):
    #     if not value:
    #         raise serializers.ValidationError("Password is required to secure credentials.")
    #     # Add password strength check if needed (using zxcvbn)
    #     # pswd_check = zxcvbn(value)
    #     # if pswd_check['score'] < 3:
    #     #     feedback = pswd_check.get('feedback', {})
    #     #     warning = feedback.get('warning', '')
    #     #     suggestions = " ".join(feedback.get('suggestions', []))
    #     #     raise serializers.ValidationError(f"Weak password: {warning} {suggestions}")
    #     return value

    # Override create/update if custom logic beyond model's save is needed
    # def create(self, validated_data):
    #     # password = validated_data.pop('password', None) # Extract password
    #     instance = PlatformInstance(**validated_data)
    #     # instance.save(password=password) # Pass password to model's save
    #     instance.save() # Call regular save as encryption is commented out
    #     return instance

    # def update(self, instance, validated_data):
    #     # password = validated_data.pop('password', None) # Extract password for potential re-encryption
    #     instance.platform = validated_data.get('platform', instance.platform)
    #     # Only update credentials if provided
    #     if 'credentials' in validated_data:
    #         instance.credentials = validated_data['credentials']
    #         # instance.save(password=password) # Pass password to model's save
    #         instance.save() # Call regular save
    #     else:
    #         instance.save() # Save other changes without touching credentials
    #     return instance


# --- Post Serializers ---

class BasePostSerializer(serializers.ModelSerializer):
    """
    Base serializer for Post models. Handles common fields.
    """
    user = serializers.PrimaryKeyRelatedField(read_only=True) # Set by view
    platform_instances = serializers.PrimaryKeyRelatedField(
        queryset=PlatformInstance.objects.all(), many=True, write_only=True
    )
    # Display platform instance details on read if needed (can be heavy)
    platform_instances_details = PlatformInstanceSerializer(source='platform_instances', many=True, read_only=True)

    class Meta:
        abstract = True # Ensure this isn't registered directly
        fields = [
            'id', 'user', 'platform_instances', 'platform_instances_details',
            'created_at', 'post_configs', 'schedule'
        ]
        read_only_fields = ['id', 'user', 'created_at', 'post_configs'] # post_configs is set by model save

    def validate_platform_instances(self, value):
        """
        Ensure platform instances belong to the requesting user.
        """
        request_user = self.context['request'].user
        for instance in value:
            if instance.user != request_user:
                raise serializers.ValidationError(
                    f"Platform instance '{instance.id}' does not belong to the current user."
                )
        return value


class PostTextSerializer(BasePostSerializer):
    """
    Serializer for Text Posts.
    """
    class Meta(BasePostSerializer.Meta):
        model = PostText
        fields = BasePostSerializer.Meta.fields + ['text']


class PostImageSerializer(BasePostSerializer):
    """
    Serializer for Image Posts. Handles image upload.
    """
    image_url = serializers.URLField(read_only=True) # Set by model save

    class Meta(BasePostSerializer.Meta):
        model = PostImage
        fields = BasePostSerializer.Meta.fields + ['caption', 'image', 'image_url']
        extra_kwargs = {
            'image': {'write_only': True, 'required': False} # Image file is write-only, URL is read-only
        }


class PostVideoSerializer(BasePostSerializer):
    """
    Serializer for Video Posts. Handles video upload.
    """
    video_url = serializers.URLField(read_only=True) # Set by model save

    class Meta(BasePostSerializer.Meta):
        model = PostVideo
        fields = BasePostSerializer.Meta.fields + ['caption', 'video', 'video_url']
        extra_kwargs = {
            'video': {'write_only': True, 'required': False} # Video file is write-only, URL is read-only
        }


class ShortFormVideoSerializer(BasePostSerializer):
    """
    Serializer for Short Form Video Posts (Reels, Shorts, etc.).
    """
    video_url = serializers.URLField(read_only=True) # Set by model save

    class Meta(BasePostSerializer.Meta):
        model = ShortFormVideo
        fields = BasePostSerializer.Meta.fields + ['caption', 'video', 'video_url']
        extra_kwargs = {
            'video': {'write_only': True, 'required': False}
        }


class StoriesSerializer(BasePostSerializer):
    """
    Serializer for Stories Posts.
    """
    media_url = serializers.URLField(read_only=True) # Set by model save

    class Meta(BasePostSerializer.Meta):
        model = Stories
        fields = BasePostSerializer.Meta.fields + ['media', 'media_url']
        extra_kwargs = {
            'media': {'write_only': True, 'required': False}
        }


# --- Other Serializers ---

class DocSerializer(serializers.ModelSerializer):
    """
    Serializer for Documentation links/content.
    """
    # Include platform details when reading
    platform = PlatformSerializer(read_only=True)
    # Use PrimaryKeyRelatedField for writing
    platform_id = serializers.PrimaryKeyRelatedField(
        queryset=Platform.objects.all(), source='platform', write_only=True, required=False, allow_null=True
    )

    class Meta:
        model = Doc
        fields = ['id', 'title', 'youtube_video', 'custom_doc', 'platform', 'platform_id']
        read_only_fields = ['id']


class NotificationSerializer(serializers.ModelSerializer):
    """
    Serializer for Notifications. Usually read-only from API perspective.
    """
    # Include relevant details for context
    platform_instance_info = serializers.StringRelatedField(source='platform_instance', read_only=True)
    user_info = serializers.StringRelatedField(source='user', read_only=True)

    class Meta:
        model = Notification
        fields = [
            'id', 'platform_instance', 'platform_instance_info', 'user', 'user_info',
            'notification', 'created_at', 'error'
        ]
        read_only_fields = [
            'id', 'platform_instance', 'platform_instance_info', 'user', 'user_info',
            'notification', 'created_at', 'error'
        ] # Notifications are typically created by the system, not API users

# --- Serializer for Triggering Actions ---
class PostActionSerializer(serializers.Serializer):
    """
    Serializer for triggering actions on posts.
    Requires the action name and potentially a password for credentials.
    """
    action = serializers.CharField(required=True)
    password = serializers.CharField(write_only=True, required=False, allow_null=True, style={'input_type': 'password'})
    delay = serializers.IntegerField(required=False, default=5, min_value=0)

    def validate_action(self, value):
        # You might want to validate the action name against allowed actions if possible
        # e.g., check if it exists in platform configs associated with the post.
        # This requires context (the post object), usually done in the view.
        allowed_actions = ["POST_IMAGE", "POST_VIDEO", "POST_TEXT", "POST_SHORT_FORM_VIDEO", "POST_STORIES"] # Example
        if value not in allowed_actions:
            raise serializers.ValidationError(f"Action '{value}' is not a recognized action type.")
        return value

