"""
Unit tests for core/vertical_selection.py (Feature 1 — Vertical Multi-View, Phase 1)

Covers:
  - Category pool builders: shared performer / studio (uniform), tags / date
    (ranked window, weight favors higher overlap / closer date)
  - _pick_side: weighted category selection, re-normalization over non-empty
    pools, the uniform-random fallback when every category is empty or every
    configured weight is 0
  - select_side_clips: end-to-end de-dup across 2 slots, the tiny-library
    repeat fallback, and the single-video (no other scenes) fallback
"""
import datetime
from unittest.mock import AsyncMock, patch

import pytest

import config
from core import vertical_selection as vsel
from tests.conftest import make_scene


def _vscene(scene_id, **kwargs):
    """A vertical (portrait) scene for candidate lists."""
    kwargs.setdefault("width", 1080)
    kwargs.setdefault("height", 1920)
    return make_scene(scene_id=scene_id, **kwargs)


# ── Category pool builders ──────────────────────────────────────────────────

class TestSharedPerformerPool:
    def test_no_performers_on_center_is_empty(self):
        center = _vscene("1", performers=[])
        candidates = [_vscene("2", performers=[{"id": "10", "name": "Jane"}])]
        assert vsel._shared_performer_pool(center, candidates) == []

    def test_matches_are_uniform_weight(self):
        center = _vscene("1", performers=[{"id": "10", "name": "Jane"}])
        match = _vscene("2", performers=[{"id": "10", "name": "Jane"}])
        no_match = _vscene("3", performers=[{"id": "99", "name": "Someone"}])
        pool = vsel._shared_performer_pool(center, [match, no_match])
        assert pool == [(match, 1.0)]

    def test_shares_any_of_multiple_performers(self):
        center = _vscene("1", performers=[{"id": "10", "name": "A"}, {"id": "11", "name": "B"}])
        match = _vscene("2", performers=[{"id": "11", "name": "B"}])
        pool = vsel._shared_performer_pool(center, [match])
        assert pool == [(match, 1.0)]


class TestSharedStudioPool:
    def test_no_studio_on_center_is_empty(self):
        center = _vscene("1", studio=None)
        candidates = [_vscene("2", studio={"id": "5", "name": "Studio"})]
        assert vsel._shared_studio_pool(center, candidates) == []

    def test_matches_same_studio_uniform_weight(self):
        center = _vscene("1", studio={"id": "5", "name": "Studio"})
        match = _vscene("2", studio={"id": "5", "name": "Studio"})
        other = _vscene("3", studio={"id": "6", "name": "Other Studio"})
        pool = vsel._shared_studio_pool(center, [match, other])
        assert pool == [(match, 1.0)]


class TestSharedTagsPool:
    def test_no_tags_on_center_is_empty(self):
        center = _vscene("1", tags=[])
        candidates = [_vscene("2", tags=[{"name": "solo"}])]
        assert vsel._shared_tags_pool(center, candidates, window=30) == []

    def test_weight_is_shared_tag_count(self):
        center = _vscene("1", tags=[{"name": "a"}, {"name": "b"}, {"name": "c"}])
        two_shared = _vscene("2", tags=[{"name": "a"}, {"name": "b"}])
        one_shared = _vscene("3", tags=[{"name": "a"}])
        pool = dict((s["id"], w) for s, w in vsel._shared_tags_pool(center, [two_shared, one_shared], window=30))
        assert pool["2"] == 2.0
        assert pool["3"] == 1.0

    def test_ranked_descending_by_overlap(self):
        center = _vscene("1", tags=[{"name": "a"}, {"name": "b"}, {"name": "c"}])
        low = _vscene("2", tags=[{"name": "a"}])
        high = _vscene("3", tags=[{"name": "a"}, {"name": "b"}, {"name": "c"}])
        pool = vsel._shared_tags_pool(center, [low, high], window=30)
        assert [s["id"] for s, _ in pool] == ["3", "2"]

    def test_zero_overlap_excluded(self):
        center = _vscene("1", tags=[{"name": "a"}])
        no_overlap = _vscene("2", tags=[{"name": "z"}])
        assert vsel._shared_tags_pool(center, [no_overlap], window=30) == []

    def test_window_truncates_to_top_n(self):
        center = _vscene("1", tags=[{"name": "a"}])
        candidates = [_vscene(str(i), tags=[{"name": "a"}]) for i in range(2, 40)]
        pool = vsel._shared_tags_pool(center, candidates, window=5)
        assert len(pool) == 5


class TestDateProximityPool:
    def test_no_date_on_center_is_empty(self):
        center = _vscene("1", date=None, created_at=None)
        candidates = [_vscene("2", date="2024-01-15")]
        assert vsel._date_proximity_pool(center, candidates, window_days=30) == []

    def test_falls_back_to_created_at(self):
        center = _vscene("1", date=None, created_at="2024-01-15T00:00:00Z")
        close = _vscene("2", date="2024-01-16")
        pool = vsel._date_proximity_pool(center, [close], window_days=30)
        assert len(pool) == 1

    def test_outside_window_excluded(self):
        center = _vscene("1", date="2024-01-01")
        far = _vscene("2", date="2024-06-01")
        assert vsel._date_proximity_pool(center, [far], window_days=30) == []

    def test_closer_date_gets_heavier_weight(self):
        center = _vscene("1", date="2024-01-01")
        near = _vscene("2", date="2024-01-02")   # 1 day away
        far = _vscene("3", date="2024-01-20")    # 19 days away
        pool = dict((s["id"], w) for s, w in vsel._date_proximity_pool(center, [near, far], window_days=30))
        assert pool["2"] > pool["3"]

    def test_exact_window_boundary_included(self):
        center = _vscene("1", date="2024-01-01")
        boundary = _vscene("2", date="2024-01-31")  # exactly 30 days
        pool = vsel._date_proximity_pool(center, [boundary], window_days=30)
        assert len(pool) == 1


# ── _build_category_pools ───────────────────────────────────────────────────

class TestBuildCategoryPools:
    def test_only_non_empty_categories_present(self):
        center = _vscene("1", performers=[{"id": "10", "name": "Jane"}], tags=[], studio=None, date=None, created_at=None)
        match = _vscene("2", performers=[{"id": "10", "name": "Jane"}])
        pools = vsel._build_category_pools(center, [match])
        assert set(pools.keys()) == {"performer"}

    def test_all_categories_present_when_all_match(self):
        center = _vscene(
            "1",
            performers=[{"id": "10", "name": "Jane"}],
            tags=[{"name": "solo"}],
            studio={"id": "5", "name": "Studio"},
            date="2024-01-01",
        )
        match = _vscene(
            "2",
            performers=[{"id": "10", "name": "Jane"}],
            tags=[{"name": "solo"}],
            studio={"id": "5", "name": "Studio"},
            date="2024-01-02",
        )
        pools = vsel._build_category_pools(center, [match])
        assert set(pools.keys()) == {"performer", "tags", "studio", "date"}

    def test_no_matches_anywhere_yields_no_pools(self):
        center = _vscene("1", performers=[], tags=[], studio=None, date=None, created_at=None)
        other = _vscene("2", performers=[{"id": "99", "name": "X"}])
        assert vsel._build_category_pools(center, [other]) == {}


# ── _pick_side ───────────────────────────────────────────────────────────────

class TestPickSide:
    def test_no_eligible_candidates_returns_none(self):
        center = _vscene("1")
        chosen, path = vsel._pick_side(center, [center], excluded_ids={"1"})
        assert chosen is None
        assert path == "no_eligible_candidates"

    def test_single_non_empty_category_is_used(self):
        center = _vscene("1", performers=[{"id": "10", "name": "Jane"}], tags=[], studio=None, date=None, created_at=None)
        match = _vscene("2", performers=[{"id": "10", "name": "Jane"}])
        chosen, path = vsel._pick_side(center, [match], excluded_ids={"1"})
        assert chosen["id"] == "2"
        assert path == "performer"

    def test_falls_back_to_uniform_random_when_all_categories_empty(self):
        center = _vscene("1", performers=[], tags=[], studio=None, date=None, created_at=None)
        unrelated = _vscene("2", performers=[])
        chosen, path = vsel._pick_side(center, [unrelated], excluded_ids={"1"})
        assert chosen["id"] == "2"
        assert path == "uniform_random_all_categories_empty"

    def test_falls_back_to_uniform_random_when_all_weights_zero(self, monkeypatch):
        monkeypatch.setattr(config, "VERTICAL_WEIGHT_PERFORMER", 0)
        monkeypatch.setattr(config, "VERTICAL_WEIGHT_TAGS", 0)
        monkeypatch.setattr(config, "VERTICAL_WEIGHT_STUDIO", 0)
        monkeypatch.setattr(config, "VERTICAL_WEIGHT_DATE", 0)
        center = _vscene("1", performers=[{"id": "10", "name": "Jane"}])
        match = _vscene("2", performers=[{"id": "10", "name": "Jane"}])
        chosen, path = vsel._pick_side(center, [match], excluded_ids={"1"})
        assert chosen["id"] == "2"
        assert path == "uniform_random_all_categories_empty"

    def test_category_selection_renormalized_over_non_empty_pools(self, monkeypatch):
        # Center only matches on performer + studio; tags/date pools are empty and
        # must not appear in the weighted-random category choice at all.
        center = _vscene(
            "1",
            performers=[{"id": "10", "name": "Jane"}],
            tags=[],
            studio={"id": "5", "name": "Studio"},
            date=None,
            created_at=None,
        )
        match = _vscene("2", performers=[{"id": "10", "name": "Jane"}], studio={"id": "5", "name": "Studio"})

        seen_categories = []
        real_choices = vsel.random.choices

        def spy_choices(population, weights=None, k=1):
            if weights and all(isinstance(item, str) for item in population):
                seen_categories.append(dict(zip(population, weights)))
            return real_choices(population, weights=weights, k=k)

        monkeypatch.setattr(vsel.random, "choices", spy_choices)
        vsel._pick_side(center, [match], excluded_ids={"1"})

        assert len(seen_categories) == 1
        assert set(seen_categories[0].keys()) == {"performer", "studio"}
        assert seen_categories[0]["performer"] == config.VERTICAL_WEIGHT_PERFORMER
        assert seen_categories[0]["studio"] == config.VERTICAL_WEIGHT_STUDIO

    def test_next_slot_recomputes_pools_after_exclusion(self):
        # Only one scene shares a tag with center; once it's excluded (already
        # picked as a side), the tags pool must come back empty for the next pick.
        center = _vscene("1", tags=[{"name": "solo"}], performers=[], studio=None, date=None, created_at=None)
        only_tag_match = _vscene("2", tags=[{"name": "solo"}])
        other = _vscene("3", tags=[])

        chosen1, path1 = vsel._pick_side(center, [only_tag_match, other], excluded_ids={"1"})
        assert chosen1["id"] == "2"
        assert path1 == "tags"

        chosen2, path2 = vsel._pick_side(center, [only_tag_match, other], excluded_ids={"1", "2"})
        assert chosen2["id"] == "3"
        assert path2 == "uniform_random_all_categories_empty"


# ── select_side_clips ────────────────────────────────────────────────────────

class TestSelectSideClips:
    async def _run(self, center, candidates):
        with patch("core.vertical_selection.stash_client.fetch_scenes", new=AsyncMock(
            return_value={"scenes": candidates}
        )):
            return await vsel.select_side_clips(center)

    async def test_two_distinct_sides_picked(self):
        center = _vscene("1", performers=[{"id": "10", "name": "Jane"}])
        side_a = _vscene("2", performers=[{"id": "10", "name": "Jane"}])
        side_b = _vscene("3", performers=[{"id": "10", "name": "Jane"}])
        sides = await self._run(center, [center, side_a, side_b])
        assert len(sides) == 2
        assert len(set(sides)) == 2
        assert set(sides) <= {"2", "3"}

    async def test_tiny_library_repeats_single_side(self):
        center = _vscene("1", performers=[{"id": "10", "name": "Jane"}])
        only_other = _vscene("2", performers=[{"id": "10", "name": "Jane"}])
        sides = await self._run(center, [center, only_other])
        assert sides == ["2", "2"]

    async def test_single_video_library_returns_empty(self):
        center = _vscene("1")
        sides = await self._run(center, [center])
        assert sides == []

    async def test_non_vertical_candidates_are_filtered_out(self):
        # fetch_scenes' raw result includes a landscape scene; select_side_clips
        # must refine it away via filter_vertical_scenes before picking.
        center = _vscene("1", performers=[{"id": "10", "name": "Jane"}])
        landscape = make_scene(scene_id="2", width=1920, height=1080, performers=[{"id": "10", "name": "Jane"}])
        only_vertical_other = _vscene("3", performers=[{"id": "10", "name": "Jane"}])
        sides = await self._run(center, [center, landscape, only_vertical_other])
        assert sides == ["3", "3"]
