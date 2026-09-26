from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView

from apps.catalog.api import BusinessProfileView
from apps.core import views as core_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # Operations
    path("health/live", core_views.health_live, name="health-live"),
    path("health/ready", core_views.health_ready, name="health-ready"),
    path("metrics", core_views.metrics, name="metrics"),
    # API v1
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path("api/v1/docs/", SpectacularRedocView.as_view(url_name="api-schema"), name="api-docs"),
    path("api/v1/auth/", include("apps.authentication.urls")),
    path("api/v1/business-profile/", BusinessProfileView.as_view(), name="business-profile"),
    path("api/v1/catalog/", include("apps.catalog.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
