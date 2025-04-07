from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views

# Create a router and register our viewsets with it.
router = DefaultRouter()
router.register(r'users', views.UserViewSet, basename='user')
router.register(r'platforms', views.PlatformViewSet, basename='platform')
router.register(r'platform-instances', views.PlatformInstanceViewSet, basename='platforminstance')
router.register(r'posts/text', views.PostTextViewSet, basename='posttext')
router.register(r'posts/image', views.PostImageViewSet, basename='postimage')
router.register(r'posts/video', views.PostVideoViewSet, basename='postvideo')
router.register(r'posts/short-form-video', views.ShortFormVideoViewSet, basename='shortformvideo')
router.register(r'posts/stories', views.StoriesViewSet, basename='stories')
router.register(r'docs', views.DocViewSet, basename='doc')
router.register(r'notifications', views.NotificationViewSet, basename='notification')


urlpatterns = [
    path('', include(router.urls)),
]

