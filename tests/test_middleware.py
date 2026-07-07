"""
Unit tests for api/middleware.py's media-path IP-authorization carve-out
(see docs/Triptych.md § Known Issues #1, resolved).

Covers `_is_image_or_video_authorized`:
  - existing /videos/ and /livetv/channels/ behavior is unchanged
  - /vertical/{session}/master.m3u8 and /vertical/{session}/seg/{name} are
    classified as media (authorized IP passes, unknown IP still 401s)
  - non-media /vertical/ endpoints (seek, stop) are NOT carved out — an
    authenticated IP alone must not bypass the API-key check for those
"""
import time

import config
import state
from api.middleware import AuthenticationMiddleware


def _scope(headers=None):
    return {"headers": headers or []}


class TestImageOrVideoAuthorized:
    def setup_method(self):
        self.mw = AuthenticationMiddleware(app=None)
        # Reset shared state between tests.
        state.authenticated_ips = {}

    def _authorize_ip(self, ip: str):
        state.authenticated_ips[ip] = time.time()

    # ── Existing behavior stays intact ──────────────────────────────────

    def test_videos_path_authorized_ip_passes(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        self._authorize_ip("10.0.0.5")
        assert self.mw._is_image_or_video_authorized(
            "/videos/scene-1/stream", "10.0.0.5", _scope()
        ) is True

    def test_videos_path_unknown_ip_fails(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        assert self.mw._is_image_or_video_authorized(
            "/videos/scene-1/stream", "10.0.0.9", _scope()
        ) is False

    def test_livetv_channel_stream_authorized_ip_passes(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        self._authorize_ip("10.0.0.5")
        assert self.mw._is_image_or_video_authorized(
            "/livetv/channels/9000/stream", "10.0.0.5", _scope()
        ) is True

    def test_livetv_channel_seg_authorized_ip_passes(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        self._authorize_ip("10.0.0.5")
        assert self.mw._is_image_or_video_authorized(
            "/livetv/channels/9000/seg/seg00001.ts", "10.0.0.5", _scope()
        ) is True

    def test_non_media_path_authorized_ip_still_fails(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        self._authorize_ip("10.0.0.5")
        assert self.mw._is_image_or_video_authorized(
            "/system/info", "10.0.0.5", _scope()
        ) is False

    # ── Vertical manifest/segment carve-out (the fix) ───────────────────

    def test_vertical_manifest_authorized_ip_passes(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        self._authorize_ip("10.0.0.5")
        assert self.mw._is_image_or_video_authorized(
            "/vertical/scene-11-abc123/master.m3u8", "10.0.0.5", _scope()
        ) is True

    def test_vertical_segment_authorized_ip_passes(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        self._authorize_ip("10.0.0.5")
        assert self.mw._is_image_or_video_authorized(
            "/vertical/scene-11-abc123/seg/seg00002.ts", "10.0.0.5", _scope()
        ) is True

    def test_vertical_manifest_unknown_ip_fails(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        assert self.mw._is_image_or_video_authorized(
            "/vertical/scene-11-abc123/master.m3u8", "10.0.0.9", _scope()
        ) is False

    def test_vertical_segment_unknown_ip_fails(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        assert self.mw._is_image_or_video_authorized(
            "/vertical/scene-11-abc123/seg/seg00002.ts", "10.0.0.9", _scope()
        ) is False

    def test_vertical_static_authenticated_ip_passes(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", ["10.0.0.5"], raising=False)
        assert self.mw._is_image_or_video_authorized(
            "/vertical/scene-11-abc123/master.m3u8", "10.0.0.5", _scope()
        ) is True

    # ── Non-media /vertical/ admin endpoints must NOT be carved out ─────

    def test_vertical_seek_authorized_ip_not_bypassed(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        self._authorize_ip("10.0.0.5")
        assert self.mw._is_image_or_video_authorized(
            "/vertical/scene-11-abc123/seek", "10.0.0.5", _scope()
        ) is False

    def test_vertical_stop_authorized_ip_not_bypassed(self, monkeypatch):
        monkeypatch.setattr(config, "AUTHENTICATED_IPS", [], raising=False)
        self._authorize_ip("10.0.0.5")
        assert self.mw._is_image_or_video_authorized(
            "/vertical/scene-11-abc123/stop", "10.0.0.5", _scope()
        ) is False
