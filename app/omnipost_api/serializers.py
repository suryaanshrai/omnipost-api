from .models import (
    User,
    PlatformInstance,
    PostText,
    PostImage,
    PostVideo,
    ShortFormVideo,
    StoryImage,
    StoryVideo
)

from rest_framework import serializers

class UserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = User
        fields = ['url', 'username', 'email', 'first_name', 'last_name',]

class PlatformInstanceSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = PlatformInstance
        fields = ['url', 'platform', 'user', 'credentials', 'instance_name',]

class PostTextSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = PostText
        fields = ['url', 'user', 'platform_instances', 'post_configs', 'schedule', 'text']

class PostImageSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = PostImage
        fields = ['url', 'user', 'platform_instances', 'post_configs', 'schedule', 'image_url', 'caption']

class PostVideoSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = PostVideo
        fields = ['url', 'user', 'platform_instances', 'post_configs', 'schedule', 'video_url', 'caption']

class ShortFormVideoSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = ShortFormVideo
        fields = ['url', 'user', 'platform_instances', 'post_configs', 'schedule', 'video_url', 'caption']

class StoryImageSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = StoryImage
        fields = ['url', 'user', 'platform_instances', 'post_configs', 'schedule', 'image_url', 'caption']

class StoryVideoSerializer(serializers.HyperlinkedModelSerializer):
    class Meta:
        model = StoryVideo
        fields = ['url', 'user', 'platform_instances', 'post_configs', 'schedule', 'video_url', 'caption']

