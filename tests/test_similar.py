"""Unit tests for core/similar.py — scored similar-scenes selection.

Covers the pure scoring (rarity weighting, facet-overlap sum, parent-tier gating)
and the async orchestrator (PORTRAIT constraint when vertical, target exclusion,
score ordering, thin-facet parent fan-out, and the no-random-fill short list).
"""
from unittest.mock import AsyncMock, patch

import pytest

from core import similar
from tests.conftest import make_scene


# ── Pure scoring ────────────────────────────────────────────────────────────

class TestRarity:
    def test_monotonic_decreasing(self):
        assert similar.rarity(5) > similar.rarity(50) > similar.rarity(700)

    def test_generic_facet_worth_about_half_a_niche_one(self):
        # a tag on ~700 scenes contributes under half the weight of one on ~5
        assert similar.rarity(700) < 0.5 * similar.rarity(5)


def _target(performers=(), tags=(), studios=()):
    return {"performers": set(performers), "tags": set(tags), "studios": set(studios)}


class TestScoreCandidate:
    def test_shared_performer_scored_by_rarity(self):
        cand = make_scene(scene_id="2", performers=[{"id": "P1", "name": "x"}])
        tgt = _target(performers=["P1"])
        corpus = {("performer", "P1"): 5}
        got = similar.score_candidate(cand, tgt, corpus, set(), set())
        assert got == pytest.approx(similar.PERFORMER_BASE * similar.rarity(5))

    def test_multi_facet_sums(self):
        cand = make_scene(scene_id="2", performers=[{"id": "P1"}],
                          tags=[{"id": "T1"}], studio={"id": "S1"})
        tgt = _target(performers=["P1"], tags=["T1"], studios=["S1"])
        corpus = {("performer", "P1"): 5, ("tag", "T1"): 8, ("studio", "S1"): 100}
        expected = (similar.PERFORMER_BASE * similar.rarity(5)
                    + similar.TAG_BASE * similar.rarity(8)
                    + similar.STUDIO_BASE * similar.rarity(100))
        assert similar.score_candidate(cand, tgt, corpus, set(), set()) == pytest.approx(expected)

    def test_generic_tag_scores_less_than_rare_tag(self):
        rare = make_scene(scene_id="2", tags=[{"id": "RARE"}])
        generic = make_scene(scene_id="3", tags=[{"id": "GEN"}])
        tgt = _target(tags=["RARE", "GEN"])
        corpus = {("tag", "RARE"): 8, ("tag", "GEN"): 700}
        s_rare = similar.score_candidate(rare, tgt, corpus, set(), set())
        s_gen = similar.score_candidate(generic, tgt, corpus, set(), set())
        assert s_rare > s_gen

    def test_no_overlap_scores_zero(self):
        cand = make_scene(scene_id="2", performers=[{"id": "OTHER"}])
        tgt = _target(performers=["P1"])
        assert similar.score_candidate(cand, tgt, {}, set(), set()) == 0.0

    def test_parent_studio_credit_only_when_exact_missing(self):
        # Candidate is in the parent studio family but a DIFFERENT exact studio.
        cand = make_scene(scene_id="2", studio={"id": "OTHER"})
        tgt = _target(studios=["S1"])
        got = similar.score_candidate(cand, tgt, {}, parent_studio_hits={"2"}, parent_tag_hits=set())
        assert got == pytest.approx(similar.PARENT_STUDIO_BASE)

    def test_parent_studio_credit_suppressed_when_exact_studio_matches(self):
        # Shares the exact studio AND is flagged as a parent hit → only exact credit, no double-pay.
        cand = make_scene(scene_id="2", studio={"id": "S1"})
        tgt = _target(studios=["S1"])
        corpus = {("studio", "S1"): 100}
        got = similar.score_candidate(cand, tgt, corpus, parent_studio_hits={"2"}, parent_tag_hits=set())
        assert got == pytest.approx(similar.STUDIO_BASE * similar.rarity(100))

    def test_shares_one_of_several_seed_studios(self):
        # Multi-seed (Next Up) target: candidate matches any studio in the set.
        cand = make_scene(scene_id="2", studio={"id": "S2"})
        tgt = _target(studios=["S1", "S2", "S3"])
        corpus = {("studio", "S2"): 40}
        got = similar.score_candidate(cand, tgt, corpus, set(), set())
        assert got == pytest.approx(similar.STUDIO_BASE * similar.rarity(40))

    def test_parent_tag_credit_only_when_no_exact_tag_shared(self):
        cand = make_scene(scene_id="2", tags=[{"id": "OTHER"}])
        tgt = _target(tags=["T1"])
        got = similar.score_candidate(cand, tgt, {}, parent_studio_hits=set(), parent_tag_hits={"2"})
        assert got == pytest.approx(similar.PARENT_TAG_BASE)


# ── Orchestrator (mocked Stash) ─────────────────────────────────────────────

def _mock_stash(target, fetch_side_effect, studio=None, tag=None):
    m = AsyncMock()
    m.get_scene = AsyncMock(return_value=target)
    m.fetch_scenes = AsyncMock(side_effect=fetch_side_effect)
    m.get_studio = AsyncMock(return_value=studio)
    m.get_tag = AsyncMock(return_value=tag)
    return m


@pytest.mark.asyncio
class TestBuildSimilarScenes:
    async def test_ranks_by_score_and_excludes_target(self):
        A = make_scene(scene_id="A", performers=[{"id": "P1"}], tags=[{"id": "T1"}])
        C = make_scene(scene_id="C", tags=[{"id": "T1"}])
        B = make_scene(scene_id="B", studio={"id": "S1"})
        target = make_scene(scene_id="TGT", performers=[{"id": "P1"}],
                            tags=[{"id": "T1"}], studio={"id": "S1"})

        def fetch(**kw):
            sf = kw["scene_filter"]
            if "performers" in sf:
                return {"count": 5, "scenes": [A]}
            if "tags" in sf:
                return {"count": 50, "scenes": [A, C]}       # >THIN → no fan-out
            if "studios" in sf:
                return {"count": 500, "scenes": [B]}          # generic → low rarity
            return {"count": 0, "scenes": []}

        with patch.object(similar, "stash_client", _mock_stash(target, fetch)):
            out = await similar.build_similar_scenes("scene-TGT", limit=12, vertical=False)
        assert [s["id"] for s in out] == ["A", "C", "B"]  # multi-facet A, rare-tag C, generic-studio B

    async def test_vertical_pushes_portrait_into_every_query(self):
        target = make_scene(scene_id="TGT", tags=[{"id": "T1"}], studio={"id": "S1"})
        seen_filters = []

        def fetch(**kw):
            seen_filters.append(kw["scene_filter"])
            return {"count": 50, "scenes": []}

        with patch.object(similar, "stash_client", _mock_stash(target, fetch)):
            await similar.build_similar_scenes("scene-TGT", limit=12, vertical=True)
        assert seen_filters and all(sf.get("orientation") == {"value": ["PORTRAIT"]} for sf in seen_filters)

    async def test_thin_studio_fans_out_to_parent(self):
        D = make_scene(scene_id="D", studio={"id": "SIB"})  # sibling under the parent
        target = make_scene(scene_id="TGT", studio={"id": "S1"})

        def fetch(**kw):
            sf = kw["scene_filter"]
            if "studios" in sf:
                if sf["studios"].get("depth") == -1:
                    return {"count": 40, "scenes": [D]}       # parent subtree
                return {"count": 3, "scenes": []}             # exact studio is THIN
            return {"count": 0, "scenes": []}

        parent_studio = {"id": "S1", "parent_studio": {"id": "PARENT"}}
        with patch.object(similar, "stash_client", _mock_stash(target, fetch, studio=parent_studio)):
            out = await similar.build_similar_scenes("scene-TGT", limit=12, vertical=False)
        assert [s["id"] for s in out] == ["D"]  # surfaced via parent fan-out, parent-tier credit

    async def test_no_facets_returns_empty(self):
        target = make_scene(scene_id="TGT", performers=[], tags=[], studio=None)
        with patch.object(similar, "stash_client", _mock_stash(target, lambda **kw: {"count": 0, "scenes": []})):
            out = await similar.build_similar_scenes("scene-TGT", limit=12, vertical=False)
        assert out == []

    async def test_no_random_fill_short_list(self):
        # One weak match, limit 12 → returns exactly the one match, never padded.
        A = make_scene(scene_id="A", tags=[{"id": "T1"}])
        target = make_scene(scene_id="TGT", tags=[{"id": "T1"}])

        def fetch(**kw):
            if "tags" in kw["scene_filter"]:
                return {"count": 50, "scenes": [A]}
            return {"count": 0, "scenes": []}

        with patch.object(similar, "stash_client", _mock_stash(target, fetch)):
            out = await similar.build_similar_scenes("scene-TGT", limit=12, vertical=False)
        assert [s["id"] for s in out] == ["A"]

    async def test_candidate_in_two_facet_queries_scored_once_and_summed(self):
        # A shares both performer and tag; it surfaces in both queries but must appear
        # once with the COMBINED score (dedup + accumulate).
        A = make_scene(scene_id="A", performers=[{"id": "P1"}], tags=[{"id": "T1"}])
        target = make_scene(scene_id="TGT", performers=[{"id": "P1"}], tags=[{"id": "T1"}])

        def fetch(**kw):
            sf = kw["scene_filter"]
            if "performers" in sf:
                return {"count": 5, "scenes": [A]}
            if "tags" in sf:
                return {"count": 8, "scenes": [A]}
            return {"count": 0, "scenes": []}

        with patch.object(similar, "stash_client", _mock_stash(target, fetch)):
            out = await similar.build_similar_scenes("scene-TGT", limit=12, vertical=False)
        assert [s["id"] for s in out] == ["A"]  # deduped to one tile

    async def test_thin_tag_fans_out_to_parent(self):
        D = make_scene(scene_id="D", tags=[{"id": "SIBTAG"}])  # under the parent tag
        target = make_scene(scene_id="TGT", tags=[{"id": "T1"}])

        def fetch(**kw):
            sf = kw["scene_filter"]
            if "tags" in sf:
                if sf["tags"].get("depth") == -1:
                    return {"count": 30, "scenes": [D]}      # parent-tag subtree
                return {"count": 4, "scenes": []}            # exact tag is THIN
            return {"count": 0, "scenes": []}

        parent_tag = {"id": "T1", "parents": [{"id": "PARENT_TAG"}]}
        with patch.object(similar, "stash_client", _mock_stash(target, fetch, tag=parent_tag)):
            out = await similar.build_similar_scenes("scene-TGT", limit=12, vertical=False)
        assert [s["id"] for s in out] == ["D"]

    async def test_performer_facet_queries_capped(self):
        # 20 performers on the target → at most PERFORMER_CAP performer queries fire.
        performers = [{"id": f"P{i}"} for i in range(20)]
        target = make_scene(scene_id="TGT", performers=performers)
        perf_queries = []

        def fetch(**kw):
            sf = kw["scene_filter"]
            if "performers" in sf:
                perf_queries.append(sf["performers"]["value"][0])
            return {"count": 5, "scenes": []}

        with patch.object(similar, "stash_client", _mock_stash(target, fetch)):
            await similar.build_similar_scenes("scene-TGT", limit=12, vertical=False)
        assert len(perf_queries) == similar.PERFORMER_CAP


@pytest.mark.asyncio
class TestBuildNextUpPool:
    def _mock(self, history, fetch_side_effect):
        m = AsyncMock()
        m.fetch_recent_watch_history = AsyncMock(return_value=history)
        m.fetch_scenes = AsyncMock(side_effect=fetch_side_effect)
        m.get_studio = AsyncMock(return_value=None)
        m.get_tag = AsyncMock(return_value=None)
        return m

    async def test_aggregates_history_excludes_watched_and_queries_unwatched(self):
        watched = make_scene(scene_id="W1", performers=[{"id": "P1"}], tags=[{"id": "T1"}])
        A = make_scene(scene_id="A", performers=[{"id": "P1"}])
        seen_unwatched = []

        def fetch(**kw):
            sf = kw["scene_filter"]
            if "performers" in sf:
                seen_unwatched.append(sf.get("play_count"))
                return {"count": 30, "scenes": [A, watched]}  # W1 must be filtered out
            return {"count": 0, "scenes": []}

        with patch.object(similar, "stash_client", self._mock([watched], fetch)):
            out = await similar.build_next_up_pool(limit=5)
        ids = [s["id"] for s in out]
        assert "A" in ids and "W1" not in ids
        assert seen_unwatched and all(pc == {"value": 0, "modifier": "EQUALS"} for pc in seen_unwatched)

    async def test_backfills_to_limit_when_scored_pool_short(self):
        watched = make_scene(scene_id="W1", performers=[{"id": "P1"}])
        A = make_scene(scene_id="A", performers=[{"id": "P1"}])
        backfill = [make_scene(scene_id=f"B{i}") for i in range(10)]

        def fetch(**kw):
            sf = kw["scene_filter"]
            if "performers" in sf:
                return {"count": 30, "scenes": [A]}
            if set(sf.keys()) == {"play_count"}:            # global backfill query
                return {"count": 100, "scenes": backfill}
            return {"count": 0, "scenes": []}

        with patch.object(similar, "stash_client", self._mock([watched], fetch)):
            out = await similar.build_next_up_pool(limit=5)
        ids = [s["id"] for s in out]
        assert len(out) == 5 and ids[0] == "A" and "W1" not in ids  # scored A first, then backfill

    async def test_empty_history_still_backfills(self):
        backfill = [make_scene(scene_id=f"B{i}") for i in range(10)]

        def fetch(**kw):
            if set(kw["scene_filter"].keys()) == {"play_count"}:
                return {"count": 100, "scenes": backfill}
            return {"count": 0, "scenes": []}

        with patch.object(similar, "stash_client", self._mock([], fetch)):
            out = await similar.build_next_up_pool(limit=5)
        assert len(out) == 5

    async def test_vertical_scopes_facet_and_backfill_queries_to_portrait(self):
        # Under the Triptych library: every facet query AND the backfill must carry
        # orientation PORTRAIT so the discovery row is vertical-only.
        watched = make_scene(scene_id="W1", performers=[{"id": "P1"}])
        A = make_scene(scene_id="A", performers=[{"id": "P1"}], width=1080, height=1920)
        backfill = [make_scene(scene_id=f"B{i}", width=1080, height=1920) for i in range(10)]
        seen_filters = []

        def fetch(**kw):
            sf = kw["scene_filter"]
            seen_filters.append(sf)
            if "performers" in sf:
                return {"count": 30, "scenes": [A]}
            if "play_count" in sf and "performers" not in sf:  # backfill
                return {"count": 100, "scenes": backfill}
            return {"count": 0, "scenes": []}

        with patch.object(similar, "stash_client", self._mock([watched], fetch)):
            out = await similar.build_next_up_pool(limit=5, vertical=True)
        assert len(out) == 5
        assert seen_filters and all(sf.get("orientation") == {"value": ["PORTRAIT"]} for sf in seen_filters)
