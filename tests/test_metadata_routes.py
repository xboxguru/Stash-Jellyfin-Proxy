"""
HTTP-layer tests for api/metadata_routes.py

Uses TestClient against the minimal Starlette app defined in conftest.py.
Stash GraphQL calls are mocked at the stash_client module level.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from core.jellyfin_mapper import encode_id
from tests.conftest import make_scene, make_performer, make_studio


# ── /items/{item_id} — scene detail ──────────────────────────────────────────

class TestItemDetailsScene:
    def test_scene_returns_200(self, client):
        scene = make_scene(scene_id="123")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            r = client.get(f"/items/{encoded}")
        assert r.status_code == 200

    def test_scene_has_type_movie(self, client):
        scene = make_scene(scene_id="123")
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.get(f"/items/{encoded}").json()
        assert data["Type"] == "Movie"

    def test_scene_title_present(self, client):
        scene = make_scene(scene_id="5", title="My Great Scene")
        encoded = encode_id("scene", "5")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=scene)):
            data = client.get(f"/items/{encoded}").json()
        assert data["Name"] == "My Great Scene"

    def test_scene_not_found_returns_404(self, client):
        encoded = encode_id("scene", "999")
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=None)):
            r = client.get(f"/items/{encoded}")
        assert r.status_code == 404

    def test_invalid_id_format_returns_400(self, client):
        # Already-decoded scene prefix but no numeric ID extractable
        # Use an encoded ID that decodes to something without digits
        with patch("core.stash_client.get_scene", new=AsyncMock(return_value=None)):
            r = client.get("/items/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")
        # Should not crash — 400 or 404 is acceptable
        assert r.status_code in (400, 404)


# ── /items/{item_id} — studio detail ─────────────────────────────────────────

class TestItemDetailsStudio:
    def _studio_response(self, studio_id="5"):
        studio = make_studio(studio_id=studio_id)
        mock_data = {"findStudio": studio}
        return mock_data

    def test_studio_returns_200(self, client):
        encoded = encode_id("studio", "5")
        with patch("core.stash_client.call_graphql", new=AsyncMock(return_value=self._studio_response())):
            r = client.get(f"/items/{encoded}")
        assert r.status_code == 200

    def test_studio_type_is_boxset(self, client):
        encoded = encode_id("studio", "5")
        with patch("core.stash_client.call_graphql", new=AsyncMock(return_value=self._studio_response())):
            data = client.get(f"/items/{encoded}").json()
        assert data["Type"] == "BoxSet"

    def test_studio_is_folder(self, client):
        encoded = encode_id("studio", "5")
        with patch("core.stash_client.call_graphql", new=AsyncMock(return_value=self._studio_response())):
            data = client.get(f"/items/{encoded}").json()
        assert data["IsFolder"] is True

    def test_studio_name_present(self, client):
        encoded = encode_id("studio", "5")
        with patch("core.stash_client.call_graphql", new=AsyncMock(return_value=self._studio_response())):
            data = client.get(f"/items/{encoded}").json()
        assert data["Name"] == "Test Studio"

    def test_studio_overview_contains_url(self, client):
        encoded = encode_id("studio", "5")
        with patch("core.stash_client.call_graphql", new=AsyncMock(return_value=self._studio_response())):
            data = client.get(f"/items/{encoded}").json()
        assert "teststudio.com" in data.get("Overview", "")

    def test_studio_not_found_returns_fallback(self, client):
        encoded = encode_id("studio", "999")
        with patch("core.stash_client.call_graphql", new=AsyncMock(return_value={"findStudio": None})):
            r = client.get(f"/items/{encoded}")
        assert r.status_code == 200
        assert r.json()["Type"] == "BoxSet"


# ── /items/{item_id} — performer detail ──────────────────────────────────────

class TestItemDetailsPerson:
    def test_performer_returns_200(self, client):
        perf = make_performer(performer_id="10")
        encoded = encode_id("person", "10")
        with patch("core.stash_client.get_performer", new=AsyncMock(return_value=perf)):
            r = client.get(f"/items/{encoded}")
        assert r.status_code == 200

    def test_performer_type_is_person(self, client):
        perf = make_performer(performer_id="10")
        encoded = encode_id("person", "10")
        with patch("core.stash_client.get_performer", new=AsyncMock(return_value=perf)):
            data = client.get(f"/items/{encoded}").json()
        assert data["Type"] == "Person"

    def test_performer_name_present(self, client):
        perf = make_performer(performer_id="10", name="Jane Doe")
        encoded = encode_id("person", "10")
        with patch("core.stash_client.get_performer", new=AsyncMock(return_value=perf)):
            data = client.get(f"/items/{encoded}").json()
        assert data["Name"] == "Jane Doe"

    def test_performer_overview_has_bio_fields(self, client):
        perf = make_performer(performer_id="10", hair_color="Blonde", country="US")
        encoded = encode_id("person", "10")
        with patch("core.stash_client.get_performer", new=AsyncMock(return_value=perf)):
            data = client.get(f"/items/{encoded}").json()
        overview = data.get("Overview", "")
        assert "Blonde" in overview
        assert "US" in overview

    def test_performer_height_converted_to_ft_in(self, client):
        perf = make_performer(performer_id="10", height_cm=165)
        encoded = encode_id("person", "10")
        with patch("core.stash_client.get_performer", new=AsyncMock(return_value=perf)):
            data = client.get(f"/items/{encoded}").json()
        overview = data.get("Overview", "")
        assert "cm" in overview
        assert "'" in overview  # ft marker

    def test_performer_premiere_date_from_birthdate(self, client):
        perf = make_performer(performer_id="10", birthdate="1990-05-20")
        encoded = encode_id("person", "10")
        with patch("core.stash_client.get_performer", new=AsyncMock(return_value=perf)):
            data = client.get(f"/items/{encoded}").json()
        assert "1990-05-20" in data.get("PremiereDate", "")

    def test_performer_not_found_returns_404(self, client):
        encoded = encode_id("person", "999")
        with patch("core.stash_client.get_performer", new=AsyncMock(return_value=None)):
            r = client.get(f"/items/{encoded}")
        assert r.status_code == 404


# ── /items/{item_id} — nav folder detail ────────────────────────────────────

class TestItemDetailsNavFolder:
    def test_root_organized_returns_200(self, client):
        encoded = encode_id("root", "organized")
        r = client.get(f"/items/{encoded}")
        assert r.status_code == 200

    def test_root_organized_is_folder(self, client):
        encoded = encode_id("root", "organized")
        data = client.get(f"/items/{encoded}").json()
        assert data["IsFolder"] is True

    def test_root_organized_name(self, client):
        encoded = encode_id("root", "organized")
        data = client.get(f"/items/{encoded}").json()
        assert "Organized" in data["Name"]

    def test_tag_folder_returns_correct_name(self, client):
        encoded = encode_id("tag", "42")
        tags = [{"id": "42", "name": "Action"}]
        with patch("core.stash_client.get_all_tags", new=AsyncMock(return_value=tags)):
            data = client.get(f"/items/{encoded}").json()
        assert data["Name"] == "Action"


# ── /genres and /tags ─────────────────────────────────────────────────────────

class TestTagsEndpoint:
    def _tags(self):
        return [
            {"id": "1", "name": "Action"},
            {"id": "2", "name": "Drama"},
            {"id": "3", "name": "Romance"},
        ]

    def test_genres_returns_200(self, client):
        with patch("core.stash_client.get_all_tags", new=AsyncMock(return_value=self._tags())):
            r = client.get("/genres")
        assert r.status_code == 200

    def test_genres_type_is_genre(self, client):
        with patch("core.stash_client.get_all_tags", new=AsyncMock(return_value=self._tags())):
            data = client.get("/genres").json()
        assert all(item["Type"] == "Genre" for item in data["Items"])

    def test_tags_type_is_tag(self, client):
        with patch("core.stash_client.get_all_tags", new=AsyncMock(return_value=self._tags())):
            data = client.get("/tags").json()
        assert all(item["Type"] == "Tag" for item in data["Items"])

    def test_total_record_count(self, client):
        with patch("core.stash_client.get_all_tags", new=AsyncMock(return_value=self._tags())):
            data = client.get("/genres").json()
        assert data["TotalRecordCount"] == 3

    def test_search_term_filters(self, client):
        with patch("core.stash_client.get_all_tags", new=AsyncMock(return_value=self._tags())):
            data = client.get("/genres?SearchTerm=action").json()
        assert data["TotalRecordCount"] == 1
        assert data["Items"][0]["Name"] == "Action"

    def test_alphabet_limit_zero_returns_count_only(self, client):
        with patch("core.stash_client.get_all_tags", new=AsyncMock(return_value=self._tags())):
            data = client.get("/genres?NameStartsWith=d&Limit=0").json()
        assert data["Items"] == []
        assert data["TotalRecordCount"] == 1  # "Drama" starts with 'd'

    def test_alphabet_name_starts_with(self, client):
        with patch("core.stash_client.get_all_tags", new=AsyncMock(return_value=self._tags())):
            data = client.get("/genres?NameStartsWith=a").json()
        names = [i["Name"] for i in data["Items"]]
        assert "Action" in names
        assert "Drama" not in names


# ── /years ────────────────────────────────────────────────────────────────────

class TestYearsEndpoint:
    def test_years_returns_200(self, client):
        r = client.get("/years")
        assert r.status_code == 200

    def test_years_are_descending(self, client):
        data = client.get("/years").json()
        years = [item["ProductionYear"] for item in data["Items"]]
        assert years == sorted(years, reverse=True)

    def test_years_go_back_to_1990(self, client):
        data = client.get("/years").json()
        years = [item["ProductionYear"] for item in data["Items"]]
        assert 1990 in years

    def test_year_type_is_year(self, client):
        data = client.get("/years").json()
        assert all(item["Type"] == "Year" for item in data["Items"])


# ── /studios ──────────────────────────────────────────────────────────────────

class TestStudiosEndpoint:
    def _studios(self):
        return [
            {"id": "1", "name": "Alpha Studio", "image_path": "http://..."},
            {"id": "2", "name": "Beta Studio", "image_path": "http://..."},
        ]

    def test_studios_returns_200(self, client):
        with patch("core.stash_client.get_all_studios", new=AsyncMock(return_value=self._studios())):
            r = client.get("/studios")
        assert r.status_code == 200

    def test_studios_total_record_count(self, client):
        with patch("core.stash_client.get_all_studios", new=AsyncMock(return_value=self._studios())):
            data = client.get("/studios").json()
        assert data["TotalRecordCount"] == 2

    def test_studios_search_filters(self, client):
        with patch("core.stash_client.get_all_studios", new=AsyncMock(return_value=self._studios())):
            data = client.get("/studios?searchterm=alpha").json()
        assert data["TotalRecordCount"] == 1
        assert data["Items"][0]["Name"] == "Alpha Studio"


# ── DELETE /items/{item_id} ───────────────────────────────────────────────────

class TestDeleteItem:
    def test_deletion_disabled_returns_403(self, client):
        import config as cfg
        cfg.ALLOW_CLIENT_DELETION = "Disabled"
        encoded = encode_id("scene", "123")
        r = client.delete(f"/items/{encoded}")
        assert r.status_code == 403

    def test_deletion_managed_success_returns_204(self, client):
        import config as cfg
        cfg.ALLOW_CLIENT_DELETION = "Managed"
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.destroy_scene", new=AsyncMock(return_value=True)):
            r = client.delete(f"/items/{encoded}")
        assert r.status_code == 204

    def test_deletion_managed_failure_returns_500(self, client):
        import config as cfg
        cfg.ALLOW_CLIENT_DELETION = "Managed"
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.destroy_scene", new=AsyncMock(return_value=False)):
            r = client.delete(f"/items/{encoded}")
        assert r.status_code == 500

    def test_deleting_non_scene_returns_403(self, client):
        import config as cfg
        cfg.ALLOW_CLIENT_DELETION = "Managed"
        encoded = encode_id("studio", "5")
        r = client.delete(f"/items/{encoded}")
        assert r.status_code == 403


# ── POST /items/{item_id} — update metadata ───────────────────────────────────

class TestUpdateItem:
    def test_update_scene_title_returns_204(self, client):
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.update_scene", new=AsyncMock(return_value=True)):
            r = client.post(f"/items/{encoded}", json={"Name": "New Title"})
        assert r.status_code == 204

    def test_update_non_scene_returns_400(self, client):
        encoded = encode_id("studio", "5")
        r = client.post(f"/items/{encoded}", json={"Name": "x"})
        assert r.status_code == 400

    def test_update_rating_clamps_to_100(self, client):
        encoded = encode_id("scene", "123")
        captured = {}

        async def capture(payload):
            captured.update(payload)
            return True

        with patch("core.stash_client.update_scene", new=capture):
            client.post(f"/items/{encoded}", json={"CriticRating": 150})
        assert captured.get("rating100") == 100

    def test_update_tags_synced(self, client):
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.ensure_tags_exist", new=AsyncMock(return_value=["1", "2"])) as mock_ensure, \
             patch("core.stash_client.update_scene", new=AsyncMock(return_value=True)):
            client.post(f"/items/{encoded}", json={"Tags": ["Comedy", "Drama"]})
        mock_ensure.assert_called_once()

    def test_update_ignores_dynamic_tags(self, client):
        encoded = encode_id("scene", "123")
        captured_tags = []

        async def capture_tags(tag_names):
            captured_tags.extend(tag_names)
            return []

        with patch("core.stash_client.ensure_tags_exist", new=capture_tags), \
             patch("core.stash_client.update_scene", new=AsyncMock(return_value=True)):
            client.post(f"/items/{encoded}", json={"Tags": ["Recently Added", "Comedy"]})
        assert "recently added" not in [t.lower() for t in captured_tags]
        assert "Comedy" in captured_tags

    def test_update_stash_failure_returns_500(self, client):
        encoded = encode_id("scene", "123")
        with patch("core.stash_client.update_scene", new=AsyncMock(return_value=False)):
            r = client.post(f"/items/{encoded}", json={"Name": "x"})
        assert r.status_code == 500


# ── GET /items/{item_id}/metadataeditor ──────────────────────────────────────

class TestMetadataEditor:
    def test_returns_200(self, client):
        encoded = encode_id("scene", "1")
        r = client.get(f"/items/{encoded}/metadataeditor")
        assert r.status_code == 200


# ── GET /items/{item_id}/images ───────────────────────────────────────────────

class TestItemImagesInfo:
    def test_returns_list_with_primary_and_backdrop(self, client):
        encoded = encode_id("scene", "1")
        data = client.get(f"/items/{encoded}/images").json()
        types = [i["ImageType"] for i in data]
        assert "Primary" in types
        assert "Backdrop" in types
