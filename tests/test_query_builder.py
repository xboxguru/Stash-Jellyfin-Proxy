"""
Unit tests for core/query_builder.py

StashQueryBuilder.build() is async, so all tests are async.
A minimal ASGI Request is constructed from a scope dict to avoid
requiring a running HTTP server.
"""
import pytest
from unittest.mock import AsyncMock, patch
from starlette.requests import Request

import config
from core.query_builder import StashQueryBuilder
from tests.conftest import make_request_scope


def make_request(query: dict = None, path: str = "/items") -> Request:
    scope = make_request_scope(path=path, query=query or {})
    return Request(scope)


# ── SYNC_LEVEL enforcement ────────────────────────────────────────────────────

class TestSyncLevel:
    async def test_everything_adds_no_filter(self):
        config.SYNC_LEVEL = "Everything"
        _, sf, _, _ = await StashQueryBuilder(make_request(), {}).build()
        assert "organized" not in sf
        assert "tags" not in sf

    async def test_organized_adds_organized_true(self):
        config.SYNC_LEVEL = "Organized"
        _, sf, _, _ = await StashQueryBuilder(make_request(), {}).build()
        assert sf.get("organized") is True

    async def test_tagged_adds_tags_not_null(self):
        config.SYNC_LEVEL = "Tagged"
        _, sf, _, _ = await StashQueryBuilder(make_request(), {}).build()
        assert sf.get("tags") == {"modifier": "NOT_NULL"}


# ── Default sort/direction ────────────────────────────────────────────────────

class TestDefaults:
    async def test_default_sort_created_at(self):
        fa, _, _, _ = await StashQueryBuilder(make_request(), {}).build()
        assert fa["sort"] == "created_at"

    async def test_default_direction_desc(self):
        fa, _, _, _ = await StashQueryBuilder(make_request(), {}).build()
        assert fa["direction"] == "DESC"


# ── Parent ID routing ─────────────────────────────────────────────────────────

class TestParentIdRouting:
    async def test_root_scenes_sets_folder_override(self):
        _, _, is_folder, _ = await StashQueryBuilder(
            make_request(), {"decoded_parent_id": "root-scenes"}
        ).build()
        assert is_folder is True

    async def test_root_organized(self):
        _, sf, is_folder, _ = await StashQueryBuilder(
            make_request(), {"decoded_parent_id": "root-organized"}
        ).build()
        assert sf.get("organized") is True
        assert is_folder is True

    async def test_root_tagged(self):
        _, sf, is_folder, _ = await StashQueryBuilder(
            make_request(), {"decoded_parent_id": "root-tagged"}
        ).build()
        assert sf["tags"]["modifier"] == "NOT_NULL"
        assert is_folder is True

    async def test_root_recent_has_date_filter(self):
        _, sf, is_folder, _ = await StashQueryBuilder(
            make_request(), {"decoded_parent_id": "root-recent"}
        ).build()
        assert "created_at" in sf
        assert sf["created_at"]["modifier"] == "GREATER_THAN"
        assert is_folder is True

    async def test_tag_id_builds_includes_filter(self):
        _, sf, is_folder, _ = await StashQueryBuilder(
            make_request(), {"decoded_parent_id": "tag-42"}
        ).build()
        assert sf["tags"]["value"] == ["42"]
        assert sf["tags"]["modifier"] == "INCLUDES"
        assert is_folder is True

    async def test_person_id_builds_performers_filter(self):
        _, sf, _, _ = await StashQueryBuilder(
            make_request(), {"decoded_parent_id": "person-99"}
        ).build()
        assert sf["performers"]["value"] == ["99"]
        assert sf["performers"]["modifier"] == "INCLUDES"

    async def test_studio_id_builds_studios_filter(self):
        _, sf, _, _ = await StashQueryBuilder(
            make_request(), {"decoded_parent_id": "studio-7"}
        ).build()
        assert sf["studios"]["value"] == ["7"]
        assert sf["studios"]["modifier"] == "INCLUDES"

    async def test_filter_id_loads_saved_filter(self):
        saved = [{"id": "3", "name": "My Filter", "object_filter": {"organized": True}}]
        with patch("core.query_builder.stash_client.get_saved_filters", new=AsyncMock(return_value=saved)):
            _, sf, is_folder, _ = await StashQueryBuilder(
                make_request(), {"decoded_parent_id": "filter-3"}
            ).build()
        assert sf.get("organized") is True
        assert is_folder is True

    async def test_unknown_filter_id_gracefully_empty(self):
        with patch("core.query_builder.stash_client.get_saved_filters", new=AsyncMock(return_value=[])):
            _, sf, is_folder, _ = await StashQueryBuilder(
                make_request(), {"decoded_parent_id": "filter-999"}
            ).build()
        assert is_folder is True  # still a folder override
        # No crash, filter may be empty or contain SYNC_LEVEL constraints only


# ── Playback state filters ────────────────────────────────────────────────────

class TestPlaybackFilters:
    async def test_is_favorite_true(self):
        req = make_request({"isFavorite": "true"})
        _, sf, _, _ = await StashQueryBuilder(req, {}).build()
        assert sf["o_counter"]["modifier"] == "GREATER_THAN"
        assert sf["o_counter"]["value"] == 0

    async def test_is_favorite_false(self):
        req = make_request({"isFavorite": "false"})
        _, sf, _, _ = await StashQueryBuilder(req, {}).build()
        assert sf["o_counter"]["modifier"] == "EQUALS"

    async def test_is_played_true(self):
        req = make_request({"isPlayed": "true"})
        _, sf, _, _ = await StashQueryBuilder(req, {}).build()
        assert sf["play_count"]["modifier"] == "GREATER_THAN"

    async def test_is_played_false(self):
        req = make_request({"isPlayed": "false"})
        _, sf, _, _ = await StashQueryBuilder(req, {}).build()
        assert sf["play_count"]["modifier"] == "EQUALS"

    async def test_is_resumable_changes_sort_and_limit(self):
        req = make_request({"Filters": "IsResumable"})
        fa, _, _, limit = await StashQueryBuilder(req, {"filters_string": "IsResumable"}).build()
        assert fa["sort"] == "updated_at"
        assert fa["direction"] == "DESC"
        assert limit == 100


# ── Sort/order mapping ────────────────────────────────────────────────────────

class TestSortMapping:
    @pytest.mark.parametrize("jf_sort,expected_stash", [
        ("Random", "random"),
        ("DateCreated", "created_at"),
        ("DatePlayed", "updated_at"),
        ("SortName", "title"),
        ("Name", "title"),
    ])
    async def test_sort_by_mapping(self, jf_sort, expected_stash):
        req = make_request({"SortBy": jf_sort})
        fa, _, _, _ = await StashQueryBuilder(req, {}).build()
        assert fa["sort"] == expected_stash

    async def test_sort_order_ascending(self):
        req = make_request({"SortOrder": "Ascending"})
        fa, _, _, _ = await StashQueryBuilder(req, {}).build()
        assert fa["direction"] == "ASC"

    async def test_sort_order_descending(self):
        req = make_request({"SortOrder": "Descending"})
        fa, _, _, _ = await StashQueryBuilder(req, {}).build()
        assert fa["direction"] == "DESC"


# ── Year filtering ────────────────────────────────────────────────────────────

class TestYearFilter:
    async def test_single_year(self):
        req = make_request({"Years": "2022"})
        _, sf, _, _ = await StashQueryBuilder(req, {}).build()
        assert sf["date"]["modifier"] == "BETWEEN"
        assert "2022-01-01" in sf["date"]["value"]
        assert "2022-12-31" in sf["date"]["value2"]

    async def test_multiple_years_uses_range(self):
        req = make_request({"Years": "2020,2022"})
        _, sf, _, _ = await StashQueryBuilder(req, {}).build()
        assert "2020-01-01" in sf["date"]["value"]
        assert "2022-12-31" in sf["date"]["value2"]


# ── Search term ───────────────────────────────────────────────────────────────

class TestSearchTerm:
    async def test_search_term_passed_to_filter_args(self):
        fa, _, _, _ = await StashQueryBuilder(
            make_request(), {"search_term": "my search query"}
        ).build()
        assert fa["q"] == "my search query"
