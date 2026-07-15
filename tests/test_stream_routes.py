"""
HTTP-layer tests for api/stream_routes.py

IMPORTANT — NO LIVE STASH WRITES:
All stash_client calls are AsyncMock. The httpx clients inside stream_routes
(stream_client) are patched for any test that would trigger an outbound
HTTP call to Stash's stream or subtitle endpoints.

Covers:
  - POST/GET /items/{id}/playbackinfo: returns MediaSources
  - GET /videos/{id}/stream: direct-play passthrough headers
  - GET /videos/{id}/subtitles/{index}/stream.srt: subtitle proxy
  - _requires_transcode: safe/unsafe codec/container combinations
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.jellyfin_mapper import encode_id
from tests.conftest import make_scene
from api.stream_routes import _requires_transcode
from api.vertical_engine import _vertical_manager


# ── _requires_transcode (pure function) ──────────────────────────────────────

class TestRequiresTranscode:
    @pytest.mark.parametrize("codec,container", [
        ("h264", "mp4"),
        ("h265", "mp4"),
        ("hevc", "mp4"),
        ("avc", "mp4"),
        ("vp9", "webm"),
        ("av1", "webm"),
        ("h264", "mov"),
        ("h264", "m4v"),
    ])
    def test_safe_codec_container_no_transcode(self, codec, container):
        scene = make_scene(video_codec=codec, fmt=container)
        assert _requires_transcode(scene) is False

    @pytest.mark.parametrize("codec,container", [
        ("mpeg4", "avi"),
        ("wmv2", "wmv"),
        ("flv1", "flv"),
        ("h264", "avi"),    # safe codec, unsafe container
        ("mpeg4", "mp4"),   # safe container, unsafe codec
    ])
    def test_unsafe_codec_or_container_requires_transcode(self, codec, container):
        scene = make_scene(video_codec=codec, fmt=container)
        assert _requires_transcode(scene) is True

    def test_none_scene_returns_false(self):
        assert _requires_transcode(None) is False

    def test_empty_scene_returns_false(self):
        assert _requires_transcode({}) is False

    def test_scene_with_no_files_returns_false(self):
        assert _requires_transcode({"files": []}) is False


# ── GET|POST /items/{id}/playbackinfo ─────────────────────────────────────────

class TestPlaybackInfo:
    def test_returns_200(self, client):
        scene = make_scene(scene_id="123")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            r = client.post(f"/items/{encoded}/playbackinfo")
        assert r.status_code == 200

    def test_has_media_sources(self, client):
        scene = make_scene(scene_id="123")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        assert "MediaSources" in data
        assert len(data["MediaSources"]) == 1

    def test_has_play_session_id(self, client):
        scene = make_scene(scene_id="123")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        assert "PlaySessionId" in data

    def test_scene_not_found_returns_404(self, client):
        encoded = encode_id("scene", "999")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=None)):
            r = client.post(f"/items/{encoded}/playbackinfo")
        assert r.status_code == 404

    def test_get_method_also_works(self, client):
        scene = make_scene(scene_id="123")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            r = client.get(f"/items/{encoded}/playbackinfo")
        assert r.status_code == 200

    def test_direct_play_codec_in_media_sources(self, client):
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        ms = data["MediaSources"][0]
        assert ms["SupportsDirectPlay"] is True

    def test_transcode_codec_in_media_sources(self, client):
        scene = make_scene(scene_id="123", video_codec="mpeg4", fmt="avi")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        ms = data["MediaSources"][0]
        assert ms["SupportsDirectPlay"] is False


# ── Feature 2 — Client-Forced Transcoding (dual-advertise) ────────────────────

class TestForcedTranscodeAdvertise:
    """PlaybackInfo advertises the Stash HLS TranscodingUrl on compatible files ONLY when the
    client explicitly forces transcode (EnableDirectPlay=false).  A normal play stays DirectPlay
    with no TranscodingUrl, so clients like Firefox Web don't deadlock (§2.10, 2026-07-15)."""

    def test_compatible_file_normal_play_no_transcode_url(self, client, monkeypatch):
        monkeypatch.setattr("config.ENABLE_FORCED_TRANSCODE", True)
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        ms = data["MediaSources"][0]
        # Direct play only; NO TranscodingUrl advertised on a normal play.
        assert ms["SupportsDirectPlay"] is True
        assert ms["SupportsDirectStream"] is True
        assert ms["DirectStreamUrl"] == f"/Videos/{encoded}/stream"
        assert ms["TranscodingSubProtocol"] == "http"
        assert "TranscodingUrl" not in ms

    def test_compatible_file_forced_transcode_advertises_hls(self, client, monkeypatch):
        # Client picked "Play with → Transcoding": PlaybackInfo body sends EnableDirectPlay=false
        # + EnableTranscoding=true → transcode-only with the HLS TranscodingUrl (Wholphin/Fladder).
        monkeypatch.setattr("config.ENABLE_FORCED_TRANSCODE", True)
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("scene", "123")
        body = {"EnableDirectPlay": False, "EnableDirectStream": False, "EnableTranscoding": True}
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/items/{encoded}/playbackinfo", json=body).json()
        ms = data["MediaSources"][0]
        assert ms["SupportsDirectPlay"] is False
        assert ms["SupportsDirectStream"] is False
        assert ms["TranscodingSubProtocol"] == "hls"
        assert ms["TranscodingUrl"] == f"/Videos/{encoded}/master.m3u8"

    def test_compatible_file_no_transcode_url_when_flag_off(self, client, monkeypatch):
        monkeypatch.setattr("config.ENABLE_FORCED_TRANSCODE", False)
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        ms = data["MediaSources"][0]
        # Reverts to today's behavior: direct play only, no transcode URL.
        assert ms["SupportsDirectPlay"] is True
        assert ms["TranscodingSubProtocol"] == "http"
        assert "TranscodingUrl" not in ms

    def test_incompatible_file_transcode_only_regardless_of_flag(self, client, monkeypatch):
        # Auto-transcode for genuinely incompatible files must never regress (§2.10).
        monkeypatch.setattr("config.ENABLE_FORCED_TRANSCODE", False)
        scene = make_scene(scene_id="123", video_codec="mpeg4", fmt="avi")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        ms = data["MediaSources"][0]
        assert ms["SupportsDirectPlay"] is False
        assert ms["TranscodingSubProtocol"] == "hls"
        assert ms["TranscodingUrl"] == f"/Videos/{encoded}/master.m3u8"


# ── Vertical Multi-View ("Triptych") compositor wiring ────────────────────────

class TestVerticalPlaybackInfo:
    """PlaybackInfo for a 'vscene-' (vertical-library) item drives the compositor."""

    def test_vertical_item_advertises_hls_transcode(self, client, monkeypatch):
        monkeypatch.setattr("config.ENABLE_VERTICAL_MULTI", True)
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("vscene", "123")  # minted by the Vertical library browse
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch.object(_vertical_manager, "ensure", new=AsyncMock(return_value=True)), \
             patch.object(_vertical_manager, "touch", new=MagicMock()):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        ms = data["MediaSources"][0]
        # Direct play disabled + HLS transcode advertised → client uses our compositor.
        assert ms["SupportsDirectPlay"] is False
        assert ms["TranscodingSubProtocol"] == "hls"
        assert ms["TranscodingUrl"].startswith("/vertical/")
        assert ms["TranscodingUrl"].endswith("/master.m3u8")
        assert data["PlaySessionId"].startswith("123-")  # {scene_id}-{nonce}

    def test_vertical_falls_back_to_single_video_when_compositor_unavailable(self, client, monkeypatch):
        # Cap hit or no side clips → ensure() returns False → normal single-video source.
        monkeypatch.setattr("config.ENABLE_VERTICAL_MULTI", True)
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("vscene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch.object(_vertical_manager, "ensure", new=AsyncMock(return_value=False)):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        ms = data["MediaSources"][0]
        assert ms["SupportsDirectPlay"] is True  # single-video direct play
        assert "/vertical/" not in ms.get("TranscodingUrl", "")

    def test_vertical_disabled_flag_plays_normally(self, client, monkeypatch):
        # Feature off → a stray vscene- id decodes to scene- and plays as normal video.
        monkeypatch.setattr("config.ENABLE_VERTICAL_MULTI", False)
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("vscene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.post(f"/items/{encoded}/playbackinfo").json()
        ms = data["MediaSources"][0]
        assert ms["SupportsDirectPlay"] is True


class TestVerticalStreamGuard:
    """A /videos/{vscene-id}/stream built straight from the id redirects to a session."""

    def test_vertical_stream_redirects_to_composite(self, client, monkeypatch):
        monkeypatch.setattr("config.ENABLE_VERTICAL_MULTI", True)
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("vscene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch.object(_vertical_manager, "ensure", new=AsyncMock(return_value=True)):
            r = client.get(f"/videos/{encoded}/stream", follow_redirects=False)
        assert r.status_code == 302
        loc = r.headers.get("location", "")
        assert loc.startswith("/vertical/") and loc.endswith("/master.m3u8")


# ── GET /videos/{id}/stream ───────────────────────────────────────────────────

class TestStreamEndpoint:
    def _make_mock_response(self, status=200, headers=None, body=b"video data"):
        """Build a fake httpx streaming response."""
        mock_r = MagicMock()
        mock_r.status_code = status
        mock_r.headers = MagicMock()
        mock_r.headers.items.return_value = (headers or {}).items()

        def fake_headers_get(key, default=None):
            return (headers or {}).get(key, default)

        mock_r.headers.get = fake_headers_get
        mock_r.headers.__contains__ = lambda self, key: key in (headers or {})

        async def fake_aiter_bytes(chunk_size=8192):
            yield body

        mock_r.aiter_bytes = fake_aiter_bytes
        mock_r.aclose = AsyncMock()
        return mock_r

    def test_stream_direct_play_returns_200(self, client):
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("scene", "123")
        mock_resp = self._make_mock_response()
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("api.stream_routes.stream_client.build_request", return_value=MagicMock()), \
             patch("api.stream_routes.stream_client.send", new=AsyncMock(return_value=mock_resp)):
            r = client.get(f"/videos/{encoded}/stream")
        assert r.status_code == 200

    def test_head_request_returns_no_body(self, client):
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("scene", "123")
        mock_resp = self._make_mock_response()
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("api.stream_routes.stream_client.build_request", return_value=MagicMock()), \
             patch("api.stream_routes.stream_client.send", new=AsyncMock(return_value=mock_resp)):
            r = client.head(f"/videos/{encoded}/stream")
        assert r.status_code == 200
        assert r.content == b""

    def test_transcode_scene_redirects_to_m3u8(self, client):
        scene = make_scene(scene_id="123", video_codec="mpeg4", fmt="avi")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            r = client.get(f"/videos/{encoded}/stream", follow_redirects=False)
        assert r.status_code == 302
        assert "master.m3u8" in r.headers.get("location", "")

    def _make_m3u8_response(self):
        """Fake Stash HLS playlist response for _rewrite_hls_playlist."""
        mock_r = MagicMock()
        mock_r.status_code = 200
        mock_r.text = "#EXTM3U\n#EXTINF:6.0,\n0.ts?apikey=secret\n#EXT-X-ENDLIST\n"
        return mock_r

    def test_m3u8_on_compatible_file_returns_rewritten_hls(self, client, monkeypatch):
        # Feature 2: a forced .m3u8 request on a compatible file serves rewritten Stash HLS,
        # not raw mp4 passthrough (the pre-feature bug).
        monkeypatch.setattr("config.ENABLE_FORCED_TRANSCODE", True)
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("api.stream_routes.stream_client.get",
                   new=AsyncMock(return_value=self._make_m3u8_response())):
            r = client.get(f"/videos/{encoded}/master.m3u8")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/x-mpegURL")
        # Segment line rewritten to our proxy /hls/ path, upstream query stripped.
        assert "/hls/0.ts" in r.text
        assert "apikey" not in r.text
        assert "#EXTM3U" in r.text

    def test_m3u8_on_compatible_file_passthrough_when_flag_off(self, client, monkeypatch):
        # Kill-switch off → a .m3u8 on a compatible file reverts to old behavior
        # (falls through to raw passthrough; no HLS rewrite).
        monkeypatch.setattr("config.ENABLE_FORCED_TRANSCODE", False)
        scene = make_scene(scene_id="123", video_codec="h264", fmt="mp4")
        encoded = encode_id("scene", "123")
        mock_resp = self._make_mock_response(body=b"raw mp4 bytes")
        hls_get = AsyncMock(return_value=self._make_m3u8_response())
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("api.stream_routes.stream_client.get", new=hls_get), \
             patch("api.stream_routes.stream_client.build_request", return_value=MagicMock()), \
             patch("api.stream_routes.stream_client.send", new=AsyncMock(return_value=mock_resp)):
            r = client.get(f"/videos/{encoded}/master.m3u8")
        assert r.status_code == 200
        # Old behavior = passthrough: HLS playlist rewrite was never invoked.
        hls_get.assert_not_awaited()
        assert r.content == b"raw mp4 bytes"


# ── GET /videos/{id}/subtitles/{index}/stream.srt ────────────────────────────

class TestSubtitleEndpoint:
    def test_returns_200_for_valid_subtitle(self, client):
        scene = make_scene(
            scene_id="123",
            captions=[{"language_code": "eng", "caption_type": "srt"}],
        )
        scene["paths"]["caption"] = "http://localhost:9999/scene/123/caption"
        encoded = encode_id("scene", "123")

        mock_r = MagicMock()
        mock_r.status_code = 200
        mock_r.content = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"

        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("api.stream_routes.stream_client.get", new=AsyncMock(return_value=mock_r)):
            r = client.get(f"/videos/{encoded}/subtitles/2/stream.srt")
        assert r.status_code == 200

    def test_subtitle_content_returned(self, client):
        srt_content = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
        scene = make_scene(
            scene_id="123",
            captions=[{"language_code": "eng", "caption_type": "srt"}],
        )
        scene["paths"]["caption"] = "http://localhost:9999/scene/123/caption"
        encoded = encode_id("scene", "123")

        mock_r = MagicMock()
        mock_r.status_code = 200
        mock_r.content = srt_content

        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("api.stream_routes.stream_client.get", new=AsyncMock(return_value=mock_r)):
            r = client.get(f"/videos/{encoded}/subtitles/2/stream.srt")
        assert srt_content in r.content

    def test_out_of_range_index_returns_404(self, client):
        scene = make_scene(
            scene_id="123",
            captions=[{"language_code": "eng", "caption_type": "srt"}],
        )
        scene["paths"]["caption"] = "http://localhost:9999/scene/123/caption"
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            # Index 5 maps to cap_index=3, but only 1 caption exists
            r = client.get(f"/videos/{encoded}/subtitles/5/stream.srt")
        assert r.status_code == 404

    def test_no_caption_path_returns_404(self, client):
        scene = make_scene(
            scene_id="123",
            captions=[{"language_code": "eng", "caption_type": "srt"}],
        )
        scene["paths"]["caption"] = None  # No caption path
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            r = client.get(f"/videos/{encoded}/subtitles/2/stream.srt")
        assert r.status_code == 404

    def test_scene_not_found_returns_404(self, client):
        encoded = encode_id("scene", "999")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=None)):
            r = client.get(f"/videos/{encoded}/subtitles/2/stream.srt")
        assert r.status_code == 404

    def test_non_scene_id_returns_404(self, client):
        encoded = encode_id("studio", "5")
        r = client.get(f"/videos/{encoded}/subtitles/2/stream.srt")
        assert r.status_code == 404

    def test_stash_error_returns_404(self, client):
        scene = make_scene(
            scene_id="123",
            captions=[{"language_code": "eng", "caption_type": "srt"}],
        )
        scene["paths"]["caption"] = "http://localhost:9999/scene/123/caption"
        encoded = encode_id("scene", "123")

        mock_r = MagicMock()
        mock_r.status_code = 500

        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)), \
             patch("api.stream_routes.stream_client.get", new=AsyncMock(return_value=mock_r)):
            r = client.get(f"/videos/{encoded}/subtitles/2/stream.srt")
        assert r.status_code == 404
