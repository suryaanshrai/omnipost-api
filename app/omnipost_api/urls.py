from django.urls import path, include
from rest_framework.routers import DefaultRouter
from . import views as omnipost_views


router = DefaultRouter()


app_name = 'omnipost_api'

urlpatterns = [
    path('', include(router.urls)),
    path('publish/', omnipost_views.PublishApiView.as_view(), name='publish'),
    path('platform_instance/', omnipost_views.CreatePlatformInstanceView.as_view(), name='platform_instance'),
    path('post/', omnipost_views.CreatePostView.as_view(), name='post'),
    path('drafts/', omnipost_views.DraftsListView.as_view(), name='drafts'),
    path('notifications', omnipost_views.ListNotificationsView.as_view(), name='notifications'),

    path('api-auth/', include('rest_framework.urls', namespace='rest_framework')),
    path('auth/', include('dj_rest_auth.urls')),
    path('auth/registration/', include('dj_rest_auth.registration.urls'))

]

