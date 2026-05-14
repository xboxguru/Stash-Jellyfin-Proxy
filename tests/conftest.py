"""
Shared fixtures for the Stash-Jellyfin-Proxy test suite.

Every test gets:
  - A clean, known config (reset_config, autouse)
  - A clean state (reset_state, autouse)
  - A Starlette test app wrapped in AuthenticationMiddleware (app fixture)
  - An authenticated TestClient pointing at that app (client fixture)
  - An unauthenticated client for testing auth failures (anon_client fixture)
"""
import sys
import os
import logging
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Register the custom TRACE log level used by middleware/stash_client before importing app modules.
_TRACE_LEVEL = 5
if not hasattr(logging.Logger, "trace"):
    logging.addLevelName(_TRACE_LEVEL, "TRACE")

    def _trace(self, message, *args, **kws):
        if self.isEnabledFor(_TRACE_LEVEL):
            self._log(_TRACE_LEVEL, message, args, **kws)

    logging.Logger.trace = _trace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import config
import state
from api.middleware import AuthenticationMiddleware
from api import auth_routes, library_routes, metadata_routes, stream_routes, image_routes, userdata_routes

# ── Constants ────────────────────────────────────────────────────────────────

TEST_API_KEY = "test-api-key"
TEST_SERVER_ID = "test-server-id"
TEST_USER = "testuser"
TEST_PASSWORD = "testpassword"
AUTH_HEADER = {"X-Emby-Token": TEST_API_KEY}

# ── App factory ──────────────────────────────────────────────────────────────

def _build_app() -> AuthenticationMiddleware:
    """
    Minimal Starlette app with all routes used by the test suite.
    No background tasks, no static files, no WebSocket.
    """
    routes = [
        # Auth (all public)
        Route("/system/info/public", auth_routes.endpoint_system_info_public, methods=["GET"]),
        Route("/system/info", auth_routes.endpoint_system_info, methods=["GET"]),
        Route("/system/ping", auth_routes.endpoint_system_ping, methods=["GET", "POST"]),
        Route("/users/public", auth_routes.endpoint_public_users, methods=["GET"]),
        Route("/users/authenticatebyname", auth_routes.endpoint_authenticate_by_name, methods=["POST"]),
        Route("/users/{user_id}", auth_routes.endpoint_user, methods=["GET"]),
        Route("/users", auth_routes.endpoint_users, methods=["GET"]),
        Route("/branding/configuration", auth_routes.endpoint_branding_configuration, methods=["GET"]),
        Route("/quickconnect/initiate", auth_routes.endpoint_quickconnect_initiate, methods=["GET", "POST"]),
        Route("/quickconnect/connect", auth_routes.endpoint_quickconnect_connect, methods=["GET"]),

        # Library
        Route("/userviews", library_routes.endpoint_views, methods=["GET"]),
        Route("/users/{user_id}/views", library_routes.endpoint_views, methods=["GET"]),
        Route("/items", library_routes.endpoint_items, methods=["GET"]),
        Route("/users/{user_id}/items", library_routes.endpoint_items, methods=["GET"]),
        Route("/search/hints", library_routes.endpoint_search_hints, methods=["GET"]),
        Route("/shows/nextup", library_routes.endpoint_next_up, methods=["GET"]),

        # Metadata (specific before generic to avoid routing conflicts)
        Route("/genres", metadata_routes.endpoint_tags, methods=["GET"]),
        Route("/tags", metadata_routes.endpoint_tags, methods=["GET"]),
        Route("/years", metadata_routes.endpoint_years, methods=["GET"]),
        Route("/studios", metadata_routes.endpoint_studios, methods=["GET"]),
        Route("/items/{item_id}/metadataeditor", metadata_routes.endpoint_metadata_editor, methods=["GET"]),
        Route("/items/{item_id}/images", metadata_routes.endpoint_item_images_info, methods=["GET"]),
        Route("/items/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
        Route("/items/{item_id}/images/{image_type}/{image_index}", image_routes.endpoint_item_image, methods=["GET"]),
        Route("/items/{item_id}", metadata_routes.endpoint_item_details, methods=["GET"]),
        Route("/items/{item_id}", metadata_routes.endpoint_delete_item, methods=["DELETE"]),
        Route("/items/{item_id}", metadata_routes.endpoint_update_item, methods=["POST"]),
        Route("/users/{user_id}/items/{item_id}/playbackinfo", stream_routes.endpoint_playback_info, methods=["POST", "GET"]),
        Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_item_details, methods=["GET"]),
        Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_delete_item, methods=["DELETE"]),

        # Streams
        Route("/items/{item_id}/playbackinfo", stream_routes.endpoint_playback_info, methods=["POST", "GET"]),
        Route("/videos/{item_id}/subtitles/{stream_index}/stream.{format}", stream_routes.endpoint_subtitle, methods=["GET"]),
        Route("/videos/{item_id}/stream", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
        Route("/videos/{item_id}/trickplay/{width}/{file_name}", image_routes.endpoint_trickplay_image, methods=["GET"]),

        # Userdata
        Route("/sessions/playing", userdata_routes.endpoint_sessions_playing, methods=["POST"]),
        Route("/sessions/playing/stopped", userdata_routes.endpoint_sessions_stopped, methods=["POST"]),
        Route("/users/{user_id}/playeditems/{item_id}", userdata_routes.endpoint_mark_played, methods=["POST"]),
        Route("/users/{user_id}/playeditems/{item_id}", userdata_routes.endpoint_mark_unplayed, methods=["DELETE"]),
        Route("/users/{user_id}/favoriteitems/{item_id}", userdata_routes.endpoint_mark_favorite, methods=["POST"]),
        Route("/users/{user_id}/favoriteitems/{item_id}", userdata_routes.endpoint_unmark_favorite, methods=["DELETE"]),
        Route("/useritems/{item_id}/userdata", userdata_routes.endpoint_update_userdata, methods=["POST"]),
    ]
    return AuthenticationMiddleware(Starlette(routes=routes))


# Built once for the whole session; config/state are reset per test by autouse fixtures.
_APP = _build_app()

# ── Autouse fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_config():
    """Restore a known config before every test."""
    config.PROXY_API_KEY = TEST_API_KEY
    config.SERVER_ID = TEST_SERVER_ID
    config.SERVER_NAME = "Test Server"
    config.SJS_USER = TEST_USER
    config.SJS_PASSWORD = TEST_PASSWORD
    config.STASH_URL = "http://localhost:9999"
    config.STASH_API_KEY = ""
    config.STASH_GRAPHQL_PATH = "/graphql"
    config.STASH_VERIFY_TLS = False
    config.STASH_TIMEOUT = 30
    config.STASH_RETRIES = 1
    config.CACHE_VERSION = 0
    config.SYNC_LEVEL = "Everything"
    config.FAVORITE_ACTION = "o_counter"
    config.ALLOW_CLIENT_DELETION = "Disabled"
    config.RECENT_DAYS = 14
    config.TRUST_PROXY_HEADERS = False
    config.TRUSTED_PROXY_IPS = []
    config.UI_ALLOWED_IPS = []
    config.REQUIRE_AUTH_FOR_CONFIG = False
    config.UI_CSRF_PROTECTION = False
    config.AUTHENTICATED_IPS = []
    config.LOG_DIR = tempfile.mkdtemp()  # empty temp dir — no logo.png present
    config.LOG_FILE = "test.log"
    config.DEFAULT_PAGE_SIZE = 50
    config.MAX_PAGE_SIZE = 200
    config.ENABLE_FILTERS = True
    config.ENABLE_ALL_TAGS = True
    config.ENABLE_TAG_FILTERS = True
    config.TAG_GROUPS = []
    config.LATEST_GROUPS = []
    config.TOP_PLAYED_RETENTION_DAYS = 30
    config.AUTH_RATE_LIMIT_MAX_ATTEMPTS = 10
    config.AUTH_RATE_LIMIT_WINDOW_MINUTES = 15
    yield


@pytest.fixture(autouse=True)
def reset_state():
    """Reset all in-memory state before every test."""
    state.authenticated_ips = {}
    state.ui_sessions = set()
    state.stats = {
        "streams_today": 0,
        "total_streams": 0,
        "unique_ips_today": set(),
        "auth_success": 0,
        "auth_failed": 0,
    }
    state.active_streams = []
    state.login_attempts = {}
    state.quick_connect_sessions = {}
    yield


# ── Client fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def app():
    return _APP


@pytest.fixture
def client(app):
    """Authenticated TestClient (includes PROXY_API_KEY in every request)."""
    return TestClient(app, headers=AUTH_HEADER, raise_server_exceptions=True)


@pytest.fixture
def anon_client(app):
    """Unauthenticated TestClient, for testing auth enforcement and public endpoints."""
    return TestClient(app, raise_server_exceptions=True)


# ── Data factories ─────────────────────────────────────────────────────────────

def make_scene(
    scene_id="123",
    title="Test Scene",
    code=None,
    date="2024-01-15",
    details="A test scene.",
    o_counter=0,
    play_count=0,
    rating100=None,
    resume_time=0.0,
    organized=False,
    created_at="2024-01-15T12:00:00Z",
    duration=3600.0,
    video_codec="h264",
    audio_codec="aac",
    frame_rate=30,
    bit_rate=5_000_000,
    width=1920,
    height=1080,
    fmt="mp4",
    size=2_000_000_000,
    basename="test_scene.mp4",
    path="/data/test_scene.mp4",
    studio=None,
    tags=None,
    performers=None,
    captions=None,
    scene_markers=None,
) -> dict:
    return {
        "id": scene_id,
        "title": title,
        "code": code,
        "date": date,
        "details": details,
        "o_counter": o_counter,
        "play_count": play_count,
        "rating100": rating100,
        "resume_time": resume_time,
        "organized": organized,
        "created_at": created_at,
        "files": [{
            "path": path,
            "duration": duration,
            "video_codec": video_codec,
            "audio_codec": audio_codec,
            "frame_rate": frame_rate,
            "bit_rate": bit_rate,
            "width": width,
            "height": height,
            "format": fmt,
            "size": size,
            "basename": basename,
        }],
        "studio": studio,
        "tags": tags or [],
        "performers": performers or [],
        "captions": captions or [],
        "paths": {"caption": None},
        "scene_markers": scene_markers or [],
    }


def make_performer(
    performer_id="10",
    name="Jane Doe",
    image_path="http://stash/performer/10/image",
    alias_list=None,
    gender="FEMALE",
    birthdate="1990-05-20",
    country="US",
    ethnicity=None,
    hair_color="Brown",
    eye_color="Blue",
    height_cm=165,
    weight=55,
    measurements=None,
    fake_tits=None,
    penis_length=None,
    circumcised=None,
    piercings=None,
    tattoos=None,
    details="Performer bio.",
    career_length="2010-present",
) -> dict:
    return {
        "id": performer_id,
        "name": name,
        "image_path": image_path,
        "alias_list": alias_list or [],
        "gender": gender,
        "birthdate": birthdate,
        "country": country,
        "ethnicity": ethnicity,
        "hair_color": hair_color,
        "eye_color": eye_color,
        "height_cm": height_cm,
        "weight": weight,
        "measurements": measurements,
        "fake_tits": fake_tits,
        "penis_length": penis_length,
        "circumcised": circumcised,
        "piercings": piercings,
        "tattoos": tattoos,
        "details": details,
        "career_length": career_length,
    }


def make_studio(
    studio_id="5",
    name="Test Studio",
    image_path="http://stash/studio/5/image",
    details="Studio description.",
    url="https://teststudio.com",
    parent_studio=None,
) -> dict:
    return {
        "id": studio_id,
        "name": name,
        "image_path": image_path,
        "details": details,
        "url": url,
        "parent_studio": parent_studio,
    }


def make_request_scope(path: str = "/items", query: dict = None) -> dict:
    """Build a minimal ASGI scope dict for Request construction in unit tests."""
    qs = urllib.parse.urlencode(query or {}).encode()
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": qs,
        "headers": [],
    }
