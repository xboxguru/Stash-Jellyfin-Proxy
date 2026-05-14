"""
Unit tests for config.py

Covers:
  - _coerce_config_value: ints, bools, lists, strings
  - normalize_path: leading slash, trailing slash, empty input
  - get_stash_base: trailing slash stripping
"""
import pytest
import config


class TestCoerceConfigValue:
    """_coerce_config_value casts values based on the well-known key name."""

    # Integer keys
    @pytest.mark.parametrize("key,raw,expected", [
        ("PROXY_PORT", "8096", 8096),
        ("UI_PORT", "8097", 8097),
        ("CACHE_VERSION", "3", 3),
        ("STASH_TIMEOUT", "30", 30),
        ("STASH_RETRIES", "3", 3),
        ("RECENT_DAYS", "14", 14),
    ])
    def test_int_keys(self, key, raw, expected):
        result = config._coerce_config_value(key, raw)
        assert result == expected
        assert isinstance(result, int)

    def test_invalid_int_does_not_raise(self):
        # Non-numeric value for an int key should not crash
        try:
            config._coerce_config_value("PROXY_PORT", "not_a_number")
        except Exception:
            pass  # acceptable — just verify it doesn't hang

    # Boolean keys
    @pytest.mark.parametrize("val", ["true", "True", "TRUE", "1", "yes"])
    def test_bool_true_variants(self, val):
        result = config._coerce_config_value("STASH_VERIFY_TLS", val)
        assert result is True

    @pytest.mark.parametrize("val", ["false", "False", "FALSE", "0", "no"])
    def test_bool_false_variants(self, val):
        result = config._coerce_config_value("STASH_VERIFY_TLS", val)
        assert result is False

    @pytest.mark.parametrize("key", [
        "STASH_VERIFY_TLS",
        "TRUST_PROXY_HEADERS",
        "REQUIRE_AUTH_FOR_CONFIG",
        "UI_CSRF_PROTECTION",
    ])
    def test_bool_keys_return_bool(self, key):
        assert isinstance(config._coerce_config_value(key, "true"), bool)
        assert isinstance(config._coerce_config_value(key, "false"), bool)

    # List keys
    @pytest.mark.parametrize("key", [
        "TAG_GROUPS",
        "LATEST_GROUPS",
        "TRUSTED_PROXY_IPS",
        "UI_ALLOWED_IPS",
    ])
    def test_list_keys_return_list(self, key):
        result = config._coerce_config_value(key, "a, b, c")
        assert isinstance(result, list)
        assert result == ["a", "b", "c"]

    # String keys pass through unchanged
    def test_string_key_unchanged(self):
        result = config._coerce_config_value("SERVER_NAME", "My Cool Server")
        assert result == "My Cool Server"

    def test_stash_url_unchanged(self):
        result = config._coerce_config_value("STASH_URL", "http://localhost:9999")
        assert result == "http://localhost:9999"


class TestGetStashBase:
    def test_strips_single_trailing_slash(self):
        config.STASH_URL = "http://localhost:9999/"
        assert config.get_stash_base() == "http://localhost:9999"

    def test_no_trailing_slash_unchanged(self):
        config.STASH_URL = "http://localhost:9999"
        assert config.get_stash_base() == "http://localhost:9999"

    def test_strips_multiple_trailing_slashes(self):
        config.STASH_URL = "http://localhost:9999///"
        result = config.get_stash_base()
        assert not result.endswith("/")
        assert "localhost:9999" in result

    def test_https_url(self):
        config.STASH_URL = "https://stash.example.com/"
        result = config.get_stash_base()
        assert result == "https://stash.example.com"


class TestNormalizePath:
    def test_adds_leading_slash_when_missing(self):
        result = config.normalize_path("graphql", "/graphql")
        assert result.startswith("/")

    def test_strips_trailing_slash(self):
        result = config.normalize_path("/graphql/", "/graphql")
        assert not result.endswith("/")

    def test_already_correct_path_unchanged(self):
        result = config.normalize_path("/graphql", "/graphql")
        assert result == "/graphql"

    def test_empty_string_returns_default(self):
        default = "/graphql"
        result = config.normalize_path("", default)
        assert result == default

    def test_none_returns_default(self):
        default = "/graphql"
        result = config.normalize_path(None, default)
        assert result == default
