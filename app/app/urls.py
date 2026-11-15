"""
URL configuration for app project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from omnipost_api import metrics_exporter

from . import health

urlpatterns = [
    path('healthz', health.healthz, name='healthz'),
    path('readyz', health.readyz, name='readyz'),
    path('metrics', metrics_exporter.metrics_view, name='prometheus-metrics'),
    path('', include('omnipost_api.urls')),
]

if settings.ENABLE_ADMIN:
    urlpatterns += [
        path('admin/', admin.site.urls),
        path('django-rq/', include('django_rq.urls')),
        path('api-auth/', include('rest_framework.urls')),
    ]

if settings.ENABLE_API_DOCS:
    urlpatterns += [
        path('schema/', SpectacularAPIView.as_view(), name='schema'),
        path('swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
        path('redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    ]

# Uploaded media when there's no S3 bucket: platforms fetch it from
# PUBLIC_BASE_URL/media/..., so it has to be served in production too.
if settings.SERVE_MEDIA:
    _prefix = settings.MEDIA_URL.lstrip('/')
    urlpatterns += [re_path(rf'^{_prefix}(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT})]
