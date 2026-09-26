from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView

from apps.analytics import views as analytics_views
from apps.catalog.api import BusinessProfileView
from apps.core import views as core_views
from apps.notifications.webhooks import whatsapp_webhook
from apps.payments.webhooks import mpesa_callback
from apps.sales.api import QuoteRequestView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("ops/reports/", analytics_views.reports, name="analytics-reports"),
    path("ops/reports/daily.csv", analytics_views.daily_csv, name="analytics-daily-csv"),
    path("ops/", include("apps.ai.urls")),
    # Operations
    path("health/live", core_views.health_live, name="health-live"),
    path("health/ready", core_views.health_ready, name="health-ready"),
    path("metrics", core_views.metrics, name="metrics"),
    # Inbound webhooks (server-to-server)
    path("webhooks/mpesa/<str:token>/", mpesa_callback, name="mpesa-callback"),
    path("webhooks/whatsapp/", whatsapp_webhook, name="whatsapp-webhook"),
    # API v1
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path("api/v1/docs/", SpectacularRedocView.as_view(url_name="api-schema"), name="api-docs"),
    path("api/v1/auth/", include("apps.authentication.urls")),
    path("api/v1/business-profile/", BusinessProfileView.as_view(), name="business-profile"),
    path("api/v1/catalog/", include("apps.catalog.urls")),
    path("api/v1/inventory/", include("apps.inventory.urls")),
    path("api/v1/quote-requests/", QuoteRequestView.as_view(), name="quote-requests"),
    path("api/v1/analytics/summary/", analytics_views.SummaryAPI.as_view(), name="analytics-summary"),
]

if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
