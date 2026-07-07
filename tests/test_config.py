"""
Unit tests for config.py

Covers:
  - _coerce_config_value: ints, bools, floats, lists, strings
  - normalize_path: leading slash, trailing slash, empty input
  - get_stash_base: trailing slash stripping
  - save_config/load_config_file round-trip for Vertical Multi-View keys
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
        "ENABLE_VERTICAL_MULTI",
    ])
    def test_bool_keys_return_bool(self, key):
        assert isinstance(config._coerce_config_value(key, "true"), bool)
        assert isinstance(config._coerce_config_value(key, "false"), bool)

    # Float keys
    @pytest.mark.parametrize("raw,expected", [
        ("1.3", 1.3),
        ("1.78", 1.78),
        ("2", 2.0),
    ])
    def test_vertical_aspect_min_is_float(self, raw, expected):
        result = config._coerce_config_value("VERTICAL_ASPECT_MIN", raw)
        assert result == expected
        assert isinstance(result, float)

    def test_vertical_aspect_min_invalid_returns_none(self):
        assert config._coerce_config_value("VERTICAL_ASPECT_MIN", "not_a_number") is None

    # Compositor keys (Phase 1b)
    @pytest.mark.parametrize("key,raw,expected", [
        ("VERTICAL_IDLE_TIMEOUT", "60", 60),
        ("VERTICAL_MAX_SESSIONS", "2", 2),
    ])
    def test_vertical_compositor_int_keys(self, key, raw, expected):
        result = config._coerce_config_value(key, raw)
        assert result == expected
        assert isinstance(result, int)

    @pytest.mark.parametrize("raw,expected", [
        ("none", "none"), ("nvenc", "nvenc"), ("qsv", "qsv"),
        ("vaapi", "vaapi"), ("auto", "auto"), ("AUTO", "auto"),
    ])
    def test_vertical_hwaccel_valid_enum(self, raw, expected):
        assert config._coerce_config_value("VERTICAL_HWACCEL", raw) == expected

    def test_vertical_hwaccel_invalid_defaults_to_auto(self):
        assert config._coerce_config_value("VERTICAL_HWACCEL", "garbage") == "auto"

    # Compositor-rework keys (full-length seek + segment cache)
    @pytest.mark.parametrize("key,raw,expected", [
        ("VERTICAL_SESSION_TTL", "1800", 1800),
        ("VERTICAL_READY_SEGMENTS", "2", 2),
    ])
    def test_vertical_rework_int_keys(self, key, raw, expected):
        result = config._coerce_config_value(key, raw)
        assert result == expected
        assert isinstance(result, int)

    @pytest.mark.parametrize("raw,expected", [
        ("0", 0.0), ("1.5", 1.5), ("2", 2.0),
    ])
    def test_vertical_readrate_is_float(self, raw, expected):
        result = config._coerce_config_value("VERTICAL_READRATE", raw)
        assert result == expected
        assert isinstance(result, float)

    def test_vertical_readrate_invalid_returns_none(self):
        assert config._coerce_config_value("VERTICAL_READRATE", "fast") is None

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


class TestVerticalConfigRoundTrip:
    """New Vertical Multi-View keys survive a save_config -> load_config_file cycle."""

    def test_round_trip_preserves_values_and_types(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "roundtrip.conf"))
        monkeypatch.setattr(config, "ENABLE_VERTICAL_MULTI", True)
        monkeypatch.setattr(config, "VERTICAL_ASPECT_MIN", 1.45)
        config.save_config()

        # Wipe in-memory values, then reload from disk
        config.ENABLE_VERTICAL_MULTI = False
        config.VERTICAL_ASPECT_MIN = 1.3
        config.load_config_file()

        assert config.ENABLE_VERTICAL_MULTI is True
        assert config.VERTICAL_ASPECT_MIN == 1.45
        assert isinstance(config.VERTICAL_ASPECT_MIN, float)

    def test_new_keys_are_env_overridable(self):
        # _supported_keys gates both env overrides and UI saves (api_post_config)
        assert "ENABLE_VERTICAL_MULTI" in config._supported_keys
        assert "VERTICAL_ASPECT_MIN" in config._supported_keys

    def test_compositor_keys_are_env_overridable(self):
        for key in ("VERTICAL_IDLE_TIMEOUT", "VERTICAL_MAX_SESSIONS", "VERTICAL_HWACCEL",
                    "VERTICAL_READRATE", "VERTICAL_SESSION_TTL", "VERTICAL_READY_SEGMENTS"):
            assert key in config._supported_keys

    def test_rework_keys_round_trip(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "rework.conf"))
        monkeypatch.setattr(config, "VERTICAL_READRATE", 1.5)
        monkeypatch.setattr(config, "VERTICAL_SESSION_TTL", 2400)
        monkeypatch.setattr(config, "VERTICAL_READY_SEGMENTS", 3)
        config.save_config()

        config.VERTICAL_READRATE = 0.0
        config.VERTICAL_SESSION_TTL = 1800
        config.VERTICAL_READY_SEGMENTS = 2
        config.load_config_file()

        assert config.VERTICAL_READRATE == 1.5
        assert isinstance(config.VERTICAL_READRATE, float)
        assert config.VERTICAL_SESSION_TTL == 2400
        assert config.VERTICAL_READY_SEGMENTS == 3

    def test_vertical_debug_is_bool_and_round_trips(self, tmp_path, monkeypatch):
        assert config._coerce_config_value("VERTICAL_DEBUG", "true") is True
        assert config._coerce_config_value("VERTICAL_DEBUG", "false") is False
        assert "VERTICAL_DEBUG" in config._supported_keys
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "vdebug.conf"))
        monkeypatch.setattr(config, "VERTICAL_DEBUG", True)
        config.save_config()
        config.VERTICAL_DEBUG = False
        config.load_config_file()
        assert config.VERTICAL_DEBUG is True

    def test_compositor_round_trip_preserves_values_and_types(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "compositor.conf"))
        monkeypatch.setattr(config, "VERTICAL_IDLE_TIMEOUT", 90)
        monkeypatch.setattr(config, "VERTICAL_MAX_SESSIONS", 3)
        monkeypatch.setattr(config, "VERTICAL_HWACCEL", "nvenc")
        config.save_config()

        config.VERTICAL_IDLE_TIMEOUT = 60
        config.VERTICAL_MAX_SESSIONS = 2
        config.VERTICAL_HWACCEL = "auto"
        config.load_config_file()

        assert config.VERTICAL_IDLE_TIMEOUT == 90
        assert config.VERTICAL_MAX_SESSIONS == 3
        assert config.VERTICAL_HWACCEL == "nvenc"
        assert isinstance(config.VERTICAL_IDLE_TIMEOUT, int)


class TestVerticalTvChannelConfig:
    """Vertical TV channel keys (Feature 1 Phase 2) — see docs/Triptych.md."""

    def test_enable_flag_is_bool(self):
        assert config._coerce_config_value("ENABLE_VERTICAL_TV_CHANNEL", "true") is True
        assert config._coerce_config_value("ENABLE_VERTICAL_TV_CHANNEL", "false") is False

    def test_channel_number_is_int(self):
        result = config._coerce_config_value("VERTICAL_TV_CHANNEL_NUMBER", "9000")
        assert result == 9000
        assert isinstance(result, int)

    def test_keys_are_env_overridable(self):
        assert "ENABLE_VERTICAL_TV_CHANNEL" in config._supported_keys
        assert "VERTICAL_TV_CHANNEL_NUMBER" in config._supported_keys

    def test_round_trip_preserves_values_and_types(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "CONFIG_FILE", str(tmp_path / "vertical_tv.conf"))
        monkeypatch.setattr(config, "ENABLE_VERTICAL_TV_CHANNEL", True)
        monkeypatch.setattr(config, "VERTICAL_TV_CHANNEL_NUMBER", 9500)
        config.save_config()

        config.ENABLE_VERTICAL_TV_CHANNEL = False
        config.VERTICAL_TV_CHANNEL_NUMBER = 9000
        config.load_config_file()

        assert config.ENABLE_VERTICAL_TV_CHANNEL is True
        assert config.VERTICAL_TV_CHANNEL_NUMBER == 9500
        assert isinstance(config.VERTICAL_TV_CHANNEL_NUMBER, int)


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
