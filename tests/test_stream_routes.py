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
