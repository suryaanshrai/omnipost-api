from django.contrib.contenttypes.models import ContentType
import datetime
from rest_framework import viewsets
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import (
    User,
    Platform,
    PlatformInstance,
    PostText,
    PostImage,
    PostVideo,
    ShortFormVideo,
    StoryImage,
    StoryVideo,
    Notification
)

# Remove commented code along with the serializers

from .serializers import (
    UserSerializer,
    PlatformSerializer,
    PlatformInstanceSerializer,
    PostTextSerializer,
    PostImageSerializer,
    PostVideoSerializer,
    ShortFormVideoSerializer,
    StoryImageSerializer,
    StoryVideoSerializer
)


class PublishApiView(APIView):
    """
    API endpoint that allows actions to be run.
    """
    def post(self, request):        
        platform_instance_ids = request.data.get('platform_instance_ids')
        post_type = request.data.get('post_type')
        post_id = request.data.get('post_id')
        password = request.data.get('password')
        delay = 5
        match post_type:
            case 'TEXT':
                post = PostText.objects.get(id=post_id)
                action = "POST_TEXT"
            case 'IMAGE':
                post = PostImage.objects.get(id=post_id)
                delay = 10
                action = "POST_IMAGE"
            case 'VIDEO':
                post = PostVideo.objects.get(id=post_id)
                delay = 30
                action = "POST_VIDEO"
            case 'SHORT_FORM_VIDEO':
                post = ShortFormVideo.objects.get(id=post_id)
                delay = 30
                action = "POST_SHORT_FORM_VIDEO"
            case 'STORY_IMAGE':
                post = StoryImage.objects.get(id=post_id)
                delay = 10
                action = "POST_STORY_IMAGE"
            case 'STORY_VIDEO':
                post = StoryVideo.objects.get(id=post_id)
                delay = 30
                action = "POST_STORY_VIDEO"
            case _:
                return Response({"error": "Invalid post type"}, status=400)
        
        if post.user != request.user:
            return Response({"error": "You do not have permission to perform this action"}, status=403)
        
        for platform_instance_id in platform_instance_ids:
            platform_instance = PlatformInstance.objects.get(id=platform_instance_id)
            post.run_action(action=action,platform_instance=platform_instance, password=password, delay=delay)
        return Response({"status": "Action executed"}, status=200)
    
class CreatePlatformInstanceView(APIView):
    """
    API endpoint that allows platform instances to be created.
    """
    def get(self, request):
        # Return a list of platforms whose instances can be created along with their required configuration
        platforms = PlatformInstance.objects.filter(user=request.user)
        return Response ([{"id":platform.id,"platform":platform.platform.name, "instance_name":platform.instance_name,} for platform in platforms], status=200)
    
    def post(self, request):
        # Create a new platform instance
        print(request.data.keys())
        platform_id = int(request.data['platform_id'])
        user = User.objects.get(id=request.user.id)
        credentials = request.data.get('credentials')
        instance_name = request.data.get('instance_name')
        password = request.data.get('password')
        
        try:
            platform = Platform.objects.get(id=platform_id)
        except Platform.DoesNotExist:
            return Response({"error": "Platform does not exist"}, status=400)
        
        platform_instance = PlatformInstance(
            platform=platform,
            user=user,
            credentials=credentials,
            instance_name=instance_name
        )
        try:
            platform_instance.save(password=password)
        except Exception as e:
            print(e)
            return Response({"error": f"Failed to create platform instance {e} "}, status=400)
        
        return Response({"status": "Platform instance created", "instance_id": platform_instance.id}, status=201)


class CreatePostView(APIView):
    """
    API endpoint that allows posts to be created.
    """
    def post(self, request):
        # Create a new post
        post_type = request.data.get('post_type')
        content = request.data.get('content')
        
        try:
            media = request.FILES.get('media')
        except KeyError:
            media = None
        
        try:
            schedule = request.data.get('schedule')
            if schedule:
                # Convert string to datetime object
                print(schedule)
                schedule = datetime.datetime.fromisoformat(schedule.replace('Z', '+00:00'))
                print(schedule)

                if schedule < datetime.datetime.now(datetime.timezone.utc):
                    return Response({"error": "Schedule time cannot be in the past"}, status=400)
        except KeyError:
            schedule = None
        

        if not request.user.is_authenticated:
            return Response({"error": "User not authenticated"}, status=403)
        
        
        post = None
        match post_type:
            case 'TEXT':
                post = PostText(user=request.user, text=content, schedule=schedule)
            case 'IMAGE':
                post = PostImage(user=request.user, caption=content, image=media, schedule=schedule)
            case 'VIDEO':
                post = PostVideo(user=request.user, caption=content, video=media, schedule=schedule)
            case 'SHORT_FORM_VIDEO':
                post = ShortFormVideo(user=request.user, caprion=content, video=media, schedule=schedule)
            case 'STORY_IMAGE':
                post = StoryImage(user=request.user,image=media, schedule=schedule)
            case 'STORY_VIDEO':
                post = StoryVideo(user=request.user,video=media, schedule=schedule)
            case _:
                return Response({"error": "Invalid post type"}, status=400)
        
        post.save()
        
        return Response({"status": "Post created", "post_id": post.id}, status=201)
    
    
    def get(self, request):
    # API endpoint to list all posts that has been published by the user
    
        # Return a list of all published posts from all post types for this user
        user = User.objects.get(id=request.user.id)
        
        # Query each model type
        text_posts = PostText.objects.filter(user=user, published=True).values()
        image_posts = PostImage.objects.filter(user=user, published=True).values()
        video_posts = PostVideo.objects.filter(user=user, published=True).values()
        short_form_videos = ShortFormVideo.objects.filter(user=user, published=True).values()
        story_images = StoryImage.objects.filter(user=user, published=True).values()
        story_videos = StoryVideo.objects.filter(user=user, published=True).values()
        
        # Add a 'post_type' field to each result
        for post in text_posts:
            post['post_type'] = 'TEXT'
        for post in image_posts:
            post['post_type'] = 'IMAGE'
        for post in video_posts:
            post['post_type'] = 'VIDEO'
        for post in short_form_videos:
            post['post_type'] = 'SHORT_FORM_VIDEO'
        for post in story_images:
            post['post_type'] = 'STORY_IMAGE'
        for post in story_videos:
            post['post_type'] = 'STORY_VIDEO'
        
        # Combine all results
        all_posts = list(text_posts) + list(image_posts) + list(video_posts) + \
                    list(short_form_videos) + list(story_images) + list(story_videos)
        
        # Sort by creation date (newest first)
        all_posts.sort(key=lambda x: x['created_at'], reverse=True)
        
        return Response(all_posts, status=200)
    

class DraftsListView(APIView):
    """
    API endpoint that allows drafts to be listed.
    """
    def get(self, request):
        # Return a list of all drafts from all post types for this user
        user = request.user
        
        # Query each model type
        text_posts = PostText.objects.filter(user=user, published=False).values()
        image_posts = PostImage.objects.filter(user=user, published=False).values()
        video_posts = PostVideo.objects.filter(user=user, published=False).values()
        short_form_videos = ShortFormVideo.objects.filter(user=user, published=False).values()
        story_images = StoryImage.objects.filter(user=user, published=False).values()
        story_videos = StoryVideo.objects.filter(user=user, published=False).values()
        
        # Add a 'post_type' field to each result
        for post in text_posts:
            post['post_type'] = 'TEXT'
        for post in image_posts:
            post['post_type'] = 'IMAGE'
        for post in video_posts:
            post['post_type'] = 'VIDEO'
        for post in short_form_videos:
            post['post_type'] = 'SHORT_FORM_VIDEO'
        for post in story_images:
            post['post_type'] = 'STORY_IMAGE'
        for post in story_videos:
            post['post_type'] = 'STORY_VIDEO'
        
        # Combine all results
        all_posts = list(text_posts) + list(image_posts) + list(video_posts) + \
                    list(short_form_videos) + list(story_images) + list(story_videos)
        
        # Sort by creation date (newest first)
        all_posts.sort(key=lambda x: x['created_at'], reverse=True)
        
        return Response(all_posts, status=200)


class ListNotificationsView(APIView):
    """
    API endpoint that allows notifications to be listed.
    """
    def get(self, request):
        # Return a list of all notifications for this user
        user = User.objects.get(id=request.user.id)
        post_id = int(request.query_params.get('post_id')) # Use request.query_params for GET requests
        post_type_str = request.query_params.get('post_type') # Use request.query_params

        if not post_id:
            return Response({"error": "Post ID is required"}, status=400)
        if not post_type_str:
            return Response({"error": "Post type is required"}, status=400)
        
        # Validate post type and get the model class
        model_class = None
        if post_type_str == 'TEXT':
            model_class = PostText
        elif post_type_str == 'IMAGE':
            model_class = PostImage
        elif post_type_str == 'VIDEO':
            model_class = PostVideo
        elif post_type_str == 'SHORT_FORM_VIDEO':
            model_class = ShortFormVideo
        elif post_type_str == 'STORY_IMAGE':
            model_class = StoryImage
        elif post_type_str == 'STORY_VIDEO':
            model_class = StoryVideo
        else:
            return Response({"error": "Invalid post type"}, status=400)

        print(request.query_params,model_class)
        try:
            # Get the ContentType for the model
            content_type_obj = ContentType.objects.get_for_model(model_class)
        except ContentType.DoesNotExist:
            return Response({"error": "Could not find content type for the given post type"}, status=500)

        print(user, content_type_obj)
        # Query notifications based on user, object_id, and content_type
        notifications = Notification.objects.filter(
            user=user, 
            object_id=post_id,
            content_type=content_type_obj
        ).values() 

        return Response(list(notifications), status=200)
