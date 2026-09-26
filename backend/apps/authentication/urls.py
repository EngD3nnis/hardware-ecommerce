from django.urls import path

from . import views

urlpatterns = [
    path("token/", views.ThrottledTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", views.ThrottledTokenRefreshView.as_view(), name="token_refresh"),
    path("token/blacklist/", views.ThrottledTokenBlacklistView.as_view(), name="token_blacklist"),
]
