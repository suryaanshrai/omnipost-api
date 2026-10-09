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
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from omnipost_api import metrics_exporter

from . import health

urlpatterns = [
    path('healthz', health.healthz, name='healthz'),
    path('readyz', health.readyz, name='readyz'),
    path('metrics', metrics_exporter.metrics_view, name='prometheus-metrics'),
    path('admin/', admin.site.urls),
    path('django-rq/', include('django_rq.urls')),
    path('', include('omnipost_api.urls')),
    path('api-auth/', include('rest_framework.urls')),
    path('schema/', SpectacularAPIView.as_view(), name='schema'),
    path('swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
]

# Local-storage media (MediaAsset.file without S3) — served by Django only
# under DEBUG so a dev frontend can actually render uploaded images; in
# production a reverse proxy or S3 serves them. static() is a no-op when
# DEBUG is off.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
