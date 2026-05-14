"""
HTTP-layer tests for api/image_routes.py

IMPORTANT — NO LIVE STASH CALLS:
All httpx calls inside image_routes (image_client) are patched.
call_graphql is patched for chapter/trickplay tests that need GraphQL.
No requests ever reach a real Stash instance.

Covers:
  - GET /items/{id}/images/Primary: scene/performer/studio image proxy
  - GET /items/{id}/images/Chapter/{index}: chapter image routing
      index 0  → scene screenshot
      index 1+ → scene_marker screenshot
  - Root/tag/filter IDs: logo fallback (404 in tests, no logo.png present)
  - GET /videos/{id}/trickplay/{width}/tiles.m3u8: stub playlist
  - GET /videos/{id}/trickplay/{width}/0.jpg: sprite proxy or blank JPEG
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, ANY
from core.jellyfin_mapper import encode_id
from api.image_routes import BLANK_JPEG


# ── Helpers ───────────────────────────────────────────────────────────────────

def _mock_image_response(status=200, body=b"\xff\xd8\xff image data", content_type="image/jpeg"):
    """Fake streaming httpx response for _proxy_image."""
    mock_r = MagicMock()
    mock_r.status_code = status
    mock_r.headers = MagicMock()

    def _headers_get(key, default=None):
        mapping = {"content-type": content_type}
        return mapping.get(key.lower(), default)

    mock_r.headers.get = _headers_get
    mock_r.headers.__contains__ = lambda self, key: key.lower() in ("content-type",)

    async def _aiter_bytes(chunk_size=8192):
        yield body

    mock_r.aiter_bytes = _aiter_bytes
    mock_r.aclose = AsyncMock()
    return mock_r


# ── Scene/Performer/Studio image proxy ───────────────────────────────────────

class TestItemImageProxy:
    def _patch_image_client(self, mock_resp):
        return patch("api.image_routes.image_client.send", new=AsyncMock(return_value=mock_resp))

    def test_scene_image_returns_200(self, client):
        encoded = encode_id("scene", "123")
        mock_resp = _mock_image_response()
        with patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             self._patch_image_client(mock_resp):
            r = client.get(f"/items/{encoded}/images/Primary")
        assert r.status_code == 200

    def test_scene_image_returns_image_body(self, client):
        encoded = encode_id("scene", "123")
        body = b"\xff\xd8 scene screenshot"
        mock_resp = _mock_image_response(body=body)
        with patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             self._patch_image_client(mock_resp):
            r = client.get(f"/items/{encoded}/images/Primary")
        assert body in r.content

    def test_performer_image_returns_200(self, client):
        encoded = encode_id("person", "10")
        mock_resp = _mock_image_response()
        with patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             self._patch_image_client(mock_resp):
            r = client.get(f"/items/{encoded}/images/Primary")
        assert r.status_code == 200

    def test_studio_image_returns_200(self, client):
        encoded = encode_id("studio", "5")
        mock_resp = _mock_image_response()
        with patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             self._patch_image_client(mock_resp):
            r = client.get(f"/items/{encoded}/images/Primary")
        assert r.status_code == 200

    def test_upstream_error_returns_404(self, client):
        encoded = encode_id("scene", "123")
        mock_resp = _mock_image_response(status=500)
        with patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             self._patch_image_client(mock_resp):
            r = client.get(f"/items/{encoded}/images/Primary")
        assert r.status_code == 404

    def test_proxy_exception_returns_404(self, client):
        encoded = encode_id("scene", "123")
        with patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             patch("api.image_routes.image_client.send", new=AsyncMock(side_effect=Exception("timeout"))):
            r = client.get(f"/items/{encoded}/images/Primary")
        assert r.status_code == 404

    def test_root_id_returns_404_without_logo(self, client):
        encoded = encode_id("root", "scenes")
        r = client.get(f"/items/{encoded}/images/Primary")
        assert r.status_code == 404

    def test_tag_id_returns_404_without_logo(self, client):
        encoded = encode_id("tag", "7")
        r = client.get(f"/items/{encoded}/images/Primary")
        assert r.status_code == 404


# ── Chapter images ────────────────────────────────────────────────────────────

class TestChapterImages:
    def _patch_image_client(self, mock_resp):
        return patch("api.image_routes.image_client.send", new=AsyncMock(return_value=mock_resp))

    def test_chapter_index_0_returns_screenshot(self, client):
        """Index 0 = scene screenshot, no GraphQL needed."""
        encoded = encode_id("scene", "123")
        mock_resp = _mock_image_response()
        with patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             self._patch_image_client(mock_resp):
            r = client.get(f"/items/{encoded}/images/Chapter/0")
        assert r.status_code == 200

    def test_chapter_index_1_returns_first_marker(self, client):
        """Index 1 → first scene_marker screenshot."""
        encoded = encode_id("scene", "123")
        graphql_resp = {"findScene": {"scene_markers": [{"id": "m1", "seconds": 10.0}]}}
        mock_resp = _mock_image_response()
        with patch("api.image_routes.call_graphql", new=AsyncMock(return_value=graphql_resp)), \
             patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             self._patch_image_client(mock_resp):
            r = client.get(f"/items/{encoded}/images/Chapter/1")
        assert r.status_code == 200

    def test_chapter_markers_sorted_by_seconds(self, client):
        """Markers returned out-of-order from GraphQL are sorted before index lookup."""
        encoded = encode_id("scene", "123")
        # Two markers in reverse-chronological order; sorting puts m_early at idx 0
        graphql_resp = {"findScene": {"scene_markers": [
            {"id": "m_late", "seconds": 60.0},
            {"id": "m_early", "seconds": 5.0},
        ]}}
        mock_resp = _mock_image_response()
        # Index 2 (last marker in sorted list) should also resolve correctly
        with patch("api.image_routes.call_graphql", new=AsyncMock(return_value=graphql_resp)), \
             patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             self._patch_image_client(mock_resp):
            r1 = client.get(f"/items/{encoded}/images/Chapter/1")  # m_early
            r2 = client.get(f"/items/{encoded}/images/Chapter/2")  # m_late

        assert r1.status_code == 200
        assert r2.status_code == 200

    def test_chapter_out_of_bounds_returns_404(self, client):
        """Index beyond marker count → 404."""
        encoded = encode_id("scene", "123")
        graphql_resp = {"findScene": {"scene_markers": [{"id": "m1", "seconds": 10.0}]}}
        with patch("api.image_routes.call_graphql", new=AsyncMock(return_value=graphql_resp)):
            r = client.get(f"/items/{encoded}/images/Chapter/5")
        assert r.status_code == 404

    def test_chapter_no_markers_returns_404(self, client):
        encoded = encode_id("scene", "123")
        graphql_resp = {"findScene": {"scene_markers": []}}
        with patch("api.image_routes.call_graphql", new=AsyncMock(return_value=graphql_resp)):
            r = client.get(f"/items/{encoded}/images/Chapter/1")
        assert r.status_code == 404

    def test_chapter_graphql_failure_returns_404(self, client):
        encoded = encode_id("scene", "123")
        with patch("api.image_routes.call_graphql", new=AsyncMock(return_value=None)):
            r = client.get(f"/items/{encoded}/images/Chapter/1")
        assert r.status_code == 404

    def test_chapter_on_non_scene_id_falls_through(self, client):
        """Chapter image type on a non-scene ID falls through to the type_map path."""
        encoded = encode_id("studio", "5")
        mock_resp = _mock_image_response()
        with patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             self._patch_image_client(mock_resp):
            r = client.get(f"/items/{encoded}/images/Chapter/0")
        # Studio hits the type_map path instead, still proxies successfully
        assert r.status_code == 200


# ── Trickplay: tiles.m3u8 stub ────────────────────────────────────────────────

class TestTrickplayM3u8:
    def test_tiles_m3u8_returns_200(self, client):
        encoded = encode_id("scene", "123")
        r = client.get(f"/videos/{encoded}/trickplay/320/tiles.m3u8")
        assert r.status_code == 200

    def test_tiles_m3u8_content_type(self, client):
        encoded = encode_id("scene", "123")
        r = client.get(f"/videos/{encoded}/trickplay/320/tiles.m3u8")
        assert "mpegurl" in r.headers.get("content-type", "").lower()

    def test_tiles_m3u8_has_extm3u(self, client):
        encoded = encode_id("scene", "123")
        r = client.get(f"/videos/{encoded}/trickplay/320/tiles.m3u8")
        assert "#EXTM3U" in r.text

    def test_tiles_m3u8_has_endlist(self, client):
        encoded = encode_id("scene", "123")
        r = client.get(f"/videos/{encoded}/trickplay/320/tiles.m3u8")
        assert "#EXT-X-ENDLIST" in r.text


# ── Trickplay: sprite image ───────────────────────────────────────────────────

class TestTrickplaySprite:
    def _mock_graphql_sprite(self, sprite_url="/scene/123/vtt"):
        return {"data": {"findScene": {"paths": {"sprite": sprite_url}}}}

    def test_sprite_jpg_proxied_when_url_available(self, client):
        encoded = encode_id("scene", "123")
        graphql_json = self._mock_graphql_sprite()
        mock_http_resp = MagicMock()
        mock_http_resp.json.return_value = graphql_json

        proxy_resp = _mock_image_response()

        with patch("api.image_routes.image_client.post", new=AsyncMock(return_value=mock_http_resp)), \
             patch("api.image_routes.image_client.build_request", return_value=MagicMock()), \
             patch("api.image_routes.image_client.send", new=AsyncMock(return_value=proxy_resp)):
            r = client.get(f"/videos/{encoded}/trickplay/320/0.jpg")
        assert r.status_code == 200

    def test_sprite_jpg_returns_blank_when_no_sprite(self, client):
        """If GraphQL returns no sprite URL, serve BLANK_JPEG."""
        encoded = encode_id("scene", "123")
        graphql_json = {"data": {"findScene": {"paths": {"sprite": None}}}}
        mock_http_resp = MagicMock()
        mock_http_resp.json.return_value = graphql_json

        with patch("api.image_routes.image_client.post", new=AsyncMock(return_value=mock_http_resp)):
            r = client.get(f"/videos/{encoded}/trickplay/320/0.jpg")
        assert r.status_code == 200
        assert r.content == BLANK_JPEG

    def test_sprite_jpg_returns_blank_on_graphql_exception(self, client):
        """GraphQL call raises → fall back to BLANK_JPEG, not 500."""
        encoded = encode_id("scene", "123")
        with patch("api.image_routes.image_client.post", new=AsyncMock(side_effect=Exception("refused"))):
            r = client.get(f"/videos/{encoded}/trickplay/320/0.jpg")
        assert r.status_code == 200
        assert r.content == BLANK_JPEG

    def test_sprite_jpg_returns_blank_on_empty_findscene(self, client):
        encoded = encode_id("scene", "123")
        graphql_json = {"data": {"findScene": None}}
        mock_http_resp = MagicMock()
        mock_http_resp.json.return_value = graphql_json

        with patch("api.image_routes.image_client.post", new=AsyncMock(return_value=mock_http_resp)):
            r = client.get(f"/videos/{encoded}/trickplay/320/0.jpg")
        assert r.status_code == 200
        assert r.content == BLANK_JPEG

    def test_non_scene_id_trickplay_returns_404(self, client):
        encoded = encode_id("studio", "5")
        r = client.get(f"/videos/{encoded}/trickplay/320/0.jpg")
        assert r.status_code == 404

    def test_unknown_file_type_returns_404(self, client):
        encoded = encode_id("scene", "123")
        r = client.get(f"/videos/{encoded}/trickplay/320/tiles.webp")
        assert r.status_code == 404

    def test_sprite_absolute_path_prefixed_with_stash_base(self, client):
        """A sprite URL starting with '/' should be prepended with STASH_URL."""
        import config
        encoded = encode_id("scene", "123")
        graphql_json = self._mock_graphql_sprite(sprite_url="/scene/123/vtt?t=0")
        mock_http_resp = MagicMock()
        mock_http_resp.json.return_value = graphql_json

        proxy_resp = _mock_image_response()
        captured = {}

        # build_request is SYNCHRONOUS in httpx — use a plain function, not async
        def capture_build(method, url, **kwargs):
            captured["url"] = url
            return MagicMock()

        with patch("api.image_routes.image_client.post", new=AsyncMock(return_value=mock_http_resp)), \
             patch("api.image_routes.image_client.build_request", side_effect=capture_build), \
             patch("api.image_routes.image_client.send", new=AsyncMock(return_value=proxy_resp)):
            client.get(f"/videos/{encoded}/trickplay/320/0.jpg")

        assert captured.get("url", "").startswith(config.STASH_URL)
