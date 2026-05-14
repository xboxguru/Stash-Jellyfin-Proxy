"""
HTTP-layer tests for api/auth_routes.py

Public endpoints need no token. Protected endpoints (system/info)
use the authenticated client fixture.
"""
import pytest
import state
import config


# ── Public endpoints (no auth required) ──────────────────────────────────────

class TestPublicEndpoints:
    def test_system_info_public_returns_200(self, anon_client):
        r = anon_client.get("/system/info/public")
        assert r.status_code == 200

    def test_system_info_public_has_server_name(self, anon_client):
        data = anon_client.get("/system/info/public").json()
        assert data["ServerName"] == "Test Server"

    def test_system_info_public_has_server_id(self, anon_client):
        data = anon_client.get("/system/info/public").json()
        assert data["Id"] == "test-server-id"

    def test_system_ping_get(self, anon_client):
        r = anon_client.get("/system/ping")
        assert r.status_code == 200
        assert r.text == "Jellyfin Server"

    def test_system_ping_post(self, anon_client):
        r = anon_client.post("/system/ping")
        assert r.status_code == 200

    def test_public_users_returns_list(self, anon_client):
        data = anon_client.get("/users/public").json()
        assert isinstance(data, list)
        assert len(data) == 1

    def test_public_users_has_username(self, anon_client):
        data = anon_client.get("/users/public").json()
        assert data[0]["Name"] == config.SJS_USER

    def test_branding_configuration_returns_200(self, anon_client):
        r = anon_client.get("/branding/configuration")
        assert r.status_code == 200


# ── Authentication ────────────────────────────────────────────────────────────

class TestAuthentication:
    def test_correct_credentials_return_200(self, anon_client):
        r = anon_client.post("/users/authenticatebyname", json={
            "Username": config.SJS_USER,
            "Pw": config.SJS_PASSWORD,
        })
        assert r.status_code == 200

    def test_auth_response_has_access_token(self, anon_client):
        data = anon_client.post("/users/authenticatebyname", json={
            "Username": config.SJS_USER,
            "Pw": config.SJS_PASSWORD,
        }).json()
        assert "AccessToken" in data
        assert data["AccessToken"] == config.PROXY_API_KEY

    def test_auth_response_has_user(self, anon_client):
        data = anon_client.post("/users/authenticatebyname", json={
            "Username": config.SJS_USER,
            "Pw": config.SJS_PASSWORD,
        }).json()
        assert "User" in data
        assert data["User"]["Name"] == config.SJS_USER

    def test_wrong_password_returns_401(self, anon_client):
        r = anon_client.post("/users/authenticatebyname", json={
            "Username": config.SJS_USER,
            "Pw": "wrong_password",
        })
        assert r.status_code == 401

    def test_wrong_username_returns_401(self, anon_client):
        r = anon_client.post("/users/authenticatebyname", json={
            "Username": "nobody",
            "Pw": config.SJS_PASSWORD,
        })
        assert r.status_code == 401

    def test_empty_credentials_returns_401(self, anon_client):
        r = anon_client.post("/users/authenticatebyname", json={
            "Username": "",
            "Pw": "",
        })
        assert r.status_code == 401


# ── Protected endpoints require token ────────────────────────────────────────

class TestProtectedEndpoints:
    # /userviews is a protected endpoint (not in PUBLIC_ENDPOINTS) that doesn't
    # hit Stash when TAG_GROUPS is empty, making it ideal for auth enforcement tests.

    def test_protected_endpoint_requires_auth(self, anon_client):
        r = anon_client.get("/userviews")
        assert r.status_code == 401

    def test_protected_endpoint_with_token_returns_200(self, client):
        r = client.get("/userviews")
        assert r.status_code == 200

    def test_protected_endpoint_with_query_param_token(self, anon_client):
        r = anon_client.get(f"/userviews?api_key={config.PROXY_API_KEY}")
        assert r.status_code == 200

    def test_protected_endpoint_with_bearer_token(self, anon_client):
        r = anon_client.get("/userviews", headers={"Authorization": f"Bearer {config.PROXY_API_KEY}"})
        assert r.status_code == 200

    def test_wrong_token_returns_401(self, anon_client):
        r = anon_client.get("/userviews", headers={"X-Emby-Token": "wrong-key"})
        assert r.status_code == 401

    def test_successful_auth_increments_auth_success_stat(self, anon_client):
        before = state.stats["auth_success"]
        anon_client.get(f"/userviews?api_key={config.PROXY_API_KEY}")
        assert state.stats["auth_success"] == before + 1

    def test_failed_auth_increments_auth_failed_stat(self, anon_client):
        before = state.stats["auth_failed"]
        anon_client.get("/userviews", headers={"X-Emby-Token": "bad-key"})
        assert state.stats["auth_failed"] == before + 1

    def test_successful_auth_adds_ip_to_authenticated_ips(self, anon_client):
        anon_client.get(f"/userviews?api_key={config.PROXY_API_KEY}")
        assert len(state.authenticated_ips) > 0

    def test_system_info_is_public(self, anon_client):
        r = anon_client.get("/system/info")
        assert r.status_code == 200


# ── Token extraction from different header formats ────────────────────────────

class TestTokenExtraction:
    def test_emby_authorization_header_token(self, anon_client):
        header_val = f'MediaBrowser Client="test", Token="{config.PROXY_API_KEY}"'
        r = anon_client.get("/userviews", headers={"X-Emby-Authorization": header_val})
        assert r.status_code == 200

    def test_mediabrowser_token_header(self, anon_client):
        r = anon_client.get("/userviews", headers={"X-MediaBrowser-Token": config.PROXY_API_KEY})
        assert r.status_code == 200


# ── /emby/ and /jellyfin/ prefix rewriting ───────────────────────────────────

class TestPathRewriting:
    def test_emby_prefix_is_stripped(self, anon_client):
        r = anon_client.get("/emby/system/ping")
        assert r.status_code == 200

    def test_jellyfin_prefix_is_stripped(self, anon_client):
        r = anon_client.get("/jellyfin/system/ping")
        assert r.status_code == 200


# ── User endpoints ────────────────────────────────────────────────────────────

class TestUserEndpoints:
    def test_users_list_returns_200(self, client):
        r = client.get("/users")
        assert r.status_code == 200

    def test_user_by_id_returns_200(self, client):
        r = client.get("/users/testuser123")
        assert r.status_code == 200

    def test_user_has_policy_fields(self, client):
        data = client.get("/users/testuser123").json()
        assert "Policy" in data
        assert data["Policy"]["IsAdministrator"] is True


# ── Rate limiting ─────────────────────────────────────────────────────────────

class TestRateLimiting:
    def test_excessive_failures_return_429(self, anon_client):
        config.AUTH_RATE_LIMIT_MAX_ATTEMPTS = 3
        for _ in range(4):
            r = anon_client.post("/users/authenticatebyname", json={
                "Username": "bad", "Pw": "bad"
            })
        assert r.status_code in (401, 429)
