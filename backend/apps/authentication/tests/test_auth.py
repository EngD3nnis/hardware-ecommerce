import pytest
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.authentication.models import User

pytestmark = pytest.mark.django_db

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def user():
    return User.objects.create_user(email="Staff@Dewmix.example", password=PASSWORD)


@pytest.fixture
def api():
    return APIClient()


def login(api, email, password=PASSWORD):
    return api.post("/api/v1/auth/token/", {"email": email, "password": password}, format="json")


class TestUserModel:
    def test_create_user_normalises_email_and_mirrors_username(self, user):
        assert user.email == "Staff@dewmix.example"
        assert user.username == user.email
        assert user.check_password(PASSWORD)

    def test_username_follows_email_on_every_save(self, user):
        user.email = "new@dewmix.example"
        user.save()
        user.refresh_from_db()
        assert user.username == "new@dewmix.example"

    def test_admin_can_add_users(self, client):
        admin = User.objects.create_superuser(email="owner@dewmix.example", password=PASSWORD)
        client.force_login(admin)
        for email in ("a@dewmix.example", "b@dewmix.example"):
            response = client.post(
                "/admin/authentication/user/add/",
                {"email": email, "password1": PASSWORD, "password2": PASSWORD, "usable_password": "true"},
            )
            assert response.status_code == 302, response.content[:2000]
        assert User.objects.get(email="b@dewmix.example").username == "b@dewmix.example"


class TestTokens:
    def test_login_returns_tokens(self, api, user):
        response = login(api, user.email)
        assert response.status_code == 200
        assert {"access", "refresh"} <= response.json().keys()

    def test_bad_password_uses_error_envelope(self, api, user):
        response = login(api, user.email, "wrong")
        assert response.status_code == 401
        body = response.json()
        assert body["error"]["code"] == "no_active_account"
        assert body["error"]["request_id"] == response["X-Request-ID"]

    def test_rotated_refresh_token_cannot_be_reused(self, api, user):
        refresh = login(api, user.email).json()["refresh"]
        first = api.post("/api/v1/auth/token/refresh/", {"refresh": refresh}, format="json")
        assert first.status_code == 200
        reused = api.post("/api/v1/auth/token/refresh/", {"refresh": refresh}, format="json")
        assert reused.status_code == 401

    def test_logout_blacklists_refresh_token(self, api, user):
        refresh = login(api, user.email).json()["refresh"]
        assert api.post("/api/v1/auth/token/blacklist/", {"refresh": refresh}, format="json").status_code == 200
        assert api.post("/api/v1/auth/token/refresh/", {"refresh": refresh}, format="json").status_code == 401

    def test_unsupported_method_uses_error_envelope(self, api):
        response = api.get("/api/v1/auth/token/")
        assert response.status_code == 405
        assert response.json()["error"]["code"] == "method_not_allowed"


class TestLoginThrottle:
    def test_login_attempts_are_rate_limited(self, api, user, monkeypatch):
        monkeypatch.setitem(ScopedRateThrottle.THROTTLE_RATES, "auth", "3/min")
        for _ in range(3):
            assert login(api, user.email, "wrong").status_code == 401
        blocked = login(api, user.email)  # even the right password is refused while throttled
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "throttled"
        assert "Retry-After" in blocked
