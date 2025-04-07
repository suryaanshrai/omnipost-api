# views.py
from rest_framework import viewsets, permissions, status, mixins
from rest_framework.decorators import action
from rest_framework.response import Response
from django.contrib.auth import get_user_model
from .models import (
    Platform, PlatformInstance, PostText, PostImage, PostVideo,
    ShortFormVideo, Stories, Doc, Notification
)
from .serializers import (
    UserSerializer, PlatformSerializer, PlatformInstanceSerializer,
    PostTextSerializer, PostImageSerializer, PostVideoSerializer,
    ShortFormVideoSerializer, StoriesSerializer, DocSerializer,
    NotificationSerializer, PostActionSerializer
)
from django.shortcuts import get_object_or_404
# from django.core.exceptions import ValidationError as DjangoValidationError # If needed for password handling

User = get_user_model()

# --- Permissions ---
class IsOwnerOrReadOnly(permissions.BasePermission):
    """
    Custom permission to only allow owners of an object to edit it.
    Assumes the model instance has a `user` attribute.
    """
    def has_object_permission(self, request, view, obj):
        # Read permissions are allowed to any request,
        # so we'll always allow GET, HEAD or OPTIONS requests.
        if request.method in permissions.SAFE_METHODS:
            return True

        # Write permissions are only allowed to the owner of the object.
        # Handle cases where obj is User itself
        if isinstance(obj, User):
            return obj == request.user
        return obj.user == request.user

class IsAdminOrReadOnly(permissions.BasePermission):
    """
    Custom permission to only allow admin users to edit objects.
    """
    def has_permission(self, request, view):
        # Read permissions are allowed to any request
        if request.method in permissions.SAFE_METHODS:
            return True
        # Write permissions are only allowed to admin users.
        return request.user and request.user.is_staff

# --- ViewSets ---

class UserViewSet(viewsets.ReadOnlyModelViewSet):
    """
    API endpoint that allows users to be viewed.
    Read-only access.
    """
    queryset = User.objects.all().order_by('-date_joined')
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAdminUser] # Only admin can list all users

    @action(detail=False, methods=['get'], permission_classes=[permissions.IsAuthenticated])
    def me(self, request):
        """
        Return the currently authenticated user's information.
        """
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)


class PlatformViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing Platforms (e.g., Twitter, Facebook).
    Configuration details are included.
    Typically managed by admins.
    """
    queryset = Platform.objects.all()
    serializer_class = PlatformSerializer
    permission_classes = [IsAdminOrReadOnly] # Only admins can create/edit platforms


class PlatformInstanceViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing Platform Instances (user's specific accounts).
    Users can manage their own instances.
    """
    serializer_class = PlatformInstanceSerializer
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrReadOnly]

    def get_queryset(self):
        """
        This view should only return instances belonging to the currently authenticated user.
        """
        return PlatformInstance.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        """
        Associate the instance with the current user on creation.
        Handle password for encryption if the model logic was active.
        """
        # password = serializer.validated_data.pop('password', None) # Extract password if field exists
        # instance = serializer.save(user=self.request.user)
        # try:
        #     instance.save(password=password) # Pass password to model's save method
        # except DjangoValidationError as e:
        #     # If model's save raises validation error (e.g., weak password)
        #     raise serializers.ValidationError(e.messages)

        # Call save without password as model logic is commented out
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        """
        Handle updates, potentially re-encrypting credentials if password is provided.
        """
        # password = serializer.validated_data.pop('password', None) # Extract password if field exists
        # instance = serializer.save()
        # try:
        #     # Only call save with password if credentials or password were part of the update
        #     if 'credentials' in serializer.validated_data or password:
        #          instance.save(password=password)
        #     else:
        #          instance.save() # Save other changes
        # except DjangoValidationError as e:
        #     raise serializers.ValidationError(e.messages)

        # Call save without password as model logic is commented out
        serializer.save()


# --- Base Post ViewSet ---
class BasePostViewSet(viewsets.ModelViewSet):
    """
    Abstract Base ViewSet for common Post model functionality.
    Handles user association and filtering.
    """
    permission_classes = [permissions.IsAuthenticated, IsOwnerOrReadOnly]

    # Define serializer_class in subclasses
    # Define model in subclasses queryset

    def get_queryset(self):
        """
        Filter posts to only show those belonging to the current user.
        Must be implemented by subclasses by defining `self.model`.
        """
        if not hasattr(self, 'model') or not self.model:
             raise NotImplementedError("Subclasses must define 'self.model'")
        return self.model.objects.filter(user=self.request.user).order_by('-created_at')

    def perform_create(self, serializer):
        """
        Associate the post with the current user.
        The serializer handles validation of platform_instances ownership.
        """
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['post'], serializer_class=PostActionSerializer)
    def run_action(self, request, pk=None):
        """
        Custom action to trigger a specific action (e.g., 'POST_TEXT') on all
        associated platform instances for this post.
        Requires password if credentials need decryption (based on model logic).
        """
        post = self.get_object() # Gets the specific post instance (ensures ownership via get_queryset/permissions)
        action_serializer = PostActionSerializer(data=request.data)

        if action_serializer.is_valid():
            action_name = action_serializer.validated_data['action']
            password = action_serializer.validated_data.get('password') # Optional password
            delay = action_serializer.validated_data.get('delay', 5)

            try:
                # Check if the action is valid for *all* associated platforms before starting
                # (More robust check might involve iterating platforms and their configs)
                # This is a basic check; model's run_action handles detailed validation per instance.
                post.run_action_on_all_platforms(
                    action=action_name,
                    password=password, # Pass password if needed by model's get_credentials
                    delay=delay
                )
                return Response(
                    {'status': f'Action "{action_name}" queued for all platforms.'},
                    status=status.HTTP_202_ACCEPTED # 202 Accepted as it's likely asynchronous
                )
            except ValueError as e:
                # Catch errors from run_action (e.g., action not defined, decryption failure)
                return Response({'error': str(e)}, status=status.HTTP_400_BAD_REQUEST)
            except Exception as e:
                # Catch unexpected errors
                # Log the error properly in a real application
                print(f"Error running action {action_name} for post {post.id}: {e}")
                return Response({'error': 'An unexpected error occurred.'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        else:
            return Response(action_serializer.errors, status=status.HTTP_400_BAD_REQUEST)


# --- Concrete Post ViewSets ---

class PostTextViewSet(BasePostViewSet):
    """ API endpoint for Text Posts. """
    serializer_class = PostTextSerializer
    model = PostText # Define the model for the base class queryset

class PostImageViewSet(BasePostViewSet):
    """ API endpoint for Image Posts. """
    serializer_class = PostImageSerializer
    model = PostImage

class PostVideoViewSet(BasePostViewSet):
    """ API endpoint for Video Posts. """
    serializer_class = PostVideoSerializer
    model = PostVideo

class ShortFormVideoViewSet(BasePostViewSet):
    """ API endpoint for Short Form Video Posts. """
    serializer_class = ShortFormVideoSerializer
    model = ShortFormVideo

class StoriesViewSet(BasePostViewSet):
    """ API endpoint for Stories Posts. """
    serializer_class = StoriesSerializer
    model = Stories


# --- Other ViewSets ---

class DocViewSet(viewsets.ModelViewSet):
    """
    API endpoint for managing Documentation links/content.
    Accessible by authenticated users, editable by admins.
    """
    queryset = Doc.objects.all()
    serializer_class = DocSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsAdminOrReadOnly] # Allow read, admin edit


class NotificationViewSet(mixins.ListModelMixin,
                          mixins.RetrieveModelMixin,
                          viewsets.GenericViewSet):
    """
    API endpoint for viewing Notifications. Read-only.
    Users can only see their own notifications.
    """
    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        """
        Filter notifications to only show those belonging to the current user.
        """
        user = self.request.user
        # Notifications can be linked directly to user OR via platform_instance owned by user
        return Notification.objects.filter(
            models.Q(user=user) | models.Q(platform_instance__user=user)
        ).distinct().order_by('-created_at')

