from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenBlacklistView, TokenObtainPairView, TokenRefreshView

# Rate limited per client IP (rate: REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["auth"])
# to slow down password guessing and token abuse.


class ThrottledTokenObtainPairView(TokenObtainPairView):
    """Login: exchange email + password for access/refresh tokens."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class ThrottledTokenRefreshView(TokenRefreshView):
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class ThrottledTokenBlacklistView(TokenBlacklistView):
    """Logout: blacklist the given refresh token."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"
