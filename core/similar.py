"""Scene affinity scoring — powers both "More Like This" (/similar) and "Next Up".

Both features answer a facet-overlap question: gather candidate scenes that share
performers / studio / tags with a set of *seed* facets, score each by weighted
overlap (rarer facets weigh more), fan out to the parent studio / tag when an exact
facet is too thin, and return the best. They differ only in orchestration:

  • ``build_similar_scenes`` — seeds are one target scene's facets; keeps the target
    out of the results; Triptych passes ``vertical=True`` so every query is PORTRAIT
    (results are vertical-only with no post-filter, so a click plays compositor-style).
  • ``build_next_up_pool`` — seeds are aggregated across recent watch history
    (frequency-capped), fetches unwatched-only, excludes already-watched scenes, and
    backfills with random unwatched scenes to fill the rail.

SRP boundary: this module decides *what makes scenes similar*. ``stash_client`` owns
GraphQL; ``api/library_routes`` maps the returned scenes to Jellyfin items and owns
the HTTP routes. The pure scoring (``rarity`` / ``score_candidate``) is unit-testable
with no I/O; only the ``build_*`` orchestrators and their helpers touch Stash.
"""
import asyncio
import logging
import random
from collections import Counter

from core import stash_client
# Shared facet-scoring primitives (single home in core/affinity — DRY with Triptych).
from core.affinity import (  # noqa: F401  (re-exported for this module's tests)
    rarity, score_candidate, ids as _ids,
    PERFORMER_BASE, TAG_BASE, STUDIO_BASE, PARENT_STUDIO_BASE, PARENT_TAG_BASE,
)

logger = logging.getLogger(__name__)

FACET_FETCH    = 75   # candidates pulled per facet (a random slice of large facets)
PERFORMER_CAP  = 8    # cap facet queries on a single heavily-cast target scene
TAG_CAP        = 10   # cap facet queries on a single heavily-tagged target scene
THIN_THRESHOLD = 20   # exact facet corpus count below this → fan out to its parent

# Next Up aggregates facets across many watched scenes, so it needs its own (looser)
# caps — kept per-type and chosen by watch *frequency*, not at random, so the facets
# you engage with most stay in.
NEXTUP_PERFORMER_CAP = 15
NEXTUP_STUDIO_CAP    = 12
NEXTUP_TAG_CAP       = 15

# SceneFilterType criterion key per facet type.
_FIELD = {"performer": "performers", "studio": "studios", "tag": "tags"}


def _cap_random(facet_ids: set, n: int) -> list:
    """Down-sample a facet-id set to n at random (all facets of one scene are equal)."""
    lst = list(facet_ids)
    return random.sample(lst, n) if len(lst) > n else lst


async def _fetch_facet(field: str, value: str, *, vertical: bool,
                       unwatched_only: bool, depth: int | None = None) -> dict:
    """One facet candidate query → the raw ``{count, scenes}`` findScenes payload.

    ``field`` is the SceneFilterType key (performers/studios/tags). ``depth`` (``-1``)
    is set for a parent-subtree walk so Stash includes the parent's descendants.
    Random sort so an oversized facet contributes an unbiased slice; PORTRAIT and the
    unwatched filter are pushed into the query itself when requested."""
    crit = {"value": [value], "modifier": "INCLUDES"}
    if depth is not None:
        crit["depth"] = depth
    scene_filter = {field: crit}
    if vertical:
        scene_filter["orientation"] = {"value": ["PORTRAIT"]}
    if unwatched_only:
        scene_filter["play_count"] = {"value": 0, "modifier": "EQUALS"}
    data = await stash_client.fetch_scenes(
        filter_args={"sort": "random", "direction": "DESC"},
        page=1, per_page=FACET_FETCH, scene_filter=scene_filter,
    )
    return data or {"count": 0, "scenes": []}


async def _parent_ids(facet_type: str, facet_id: str) -> list:
    """Parent studio id (single) or parent tag ids (possibly several) for fan-out."""
    if facet_type == "studio":
        studio = await stash_client.get_studio(facet_id)
        parent = (studio or {}).get("parent_studio") or {}
        return [parent["id"]] if parent.get("id") else []
    tag = await stash_client.get_tag(facet_id)
    return [p["id"] for p in (tag or {}).get("parents", []) if p.get("id")]


async def _collect(seeds: list, *, vertical: bool, unwatched_only: bool) -> tuple:
    """Fire exact-facet queries for every ``(type, id)`` seed, record corpus counts,
    union the candidates, then fan out any thin studio/tag to its parent subtree.

    Returns ``(corpus, by_id, parent_studio_hits, parent_tag_hits)``.
    """
    fetched = await asyncio.gather(
        *[_fetch_facet(_FIELD[ft], fid, vertical=vertical, unwatched_only=unwatched_only)
          for ft, fid in seeds]
    )
    corpus: dict = {}
    by_id: dict = {}
    for (ft, fid), data in zip(seeds, fetched):
        corpus[(ft, fid)] = data.get("count", 0)
        for s in data.get("scenes", []):
            by_id.setdefault(s["id"], s)

    parent_studio_hits: set = set()
    parent_tag_hits: set = set()
    thin = [(ft, fid) for (ft, fid) in seeds
            if ft in ("studio", "tag") and corpus.get((ft, fid), 0) < THIN_THRESHOLD]
    if thin:
        parent_lists = await asyncio.gather(*[_parent_ids(ft, fid) for ft, fid in thin])
        parent_seeds = [(ft, pid) for (ft, _fid), pids in zip(thin, parent_lists) for pid in pids]
        if parent_seeds:
            pdata = await asyncio.gather(
                *[_fetch_facet(_FIELD[ft], pid, vertical=vertical,
                               unwatched_only=unwatched_only, depth=-1)
                  for ft, pid in parent_seeds]
            )
            for (ft, _pid), data in zip(parent_seeds, pdata):
                hits = parent_studio_hits if ft == "studio" else parent_tag_hits
                for s in data.get("scenes", []):
                    by_id.setdefault(s["id"], s)
                    hits.add(s["id"])
    return corpus, by_id, parent_studio_hits, parent_tag_hits


def _rank(by_id: dict, target: dict, corpus: dict, parent_studio_hits: set,
          parent_tag_hits: set, *, exclude_ids: set, limit: int) -> list:
    """Score every candidate, drop excluded/zero-score, sort by score DESC with a
    random tiebreak (shuffle then stable sort), and truncate to ``limit``."""
    for cid in exclude_ids:
        by_id.pop(cid, None)
    scored = [
        (score_candidate(c, target, corpus, parent_studio_hits, parent_tag_hits), c)
        for c in by_id.values()
    ]
    scored = [(s, c) for s, c in scored if s > 0]
    random.shuffle(scored)                            # random tiebreak…
    scored.sort(key=lambda sc: sc[0], reverse=True)   # …under a stable score sort
    return [c for _s, c in scored[:limit]]


async def build_similar_scenes(scene_id: str, limit: int = 12, vertical: bool = False) -> list:
    """Up to ``limit`` scenes similar to ``scene_id``, best-match first.

    ``vertical`` constrains every query to PORTRAIT (Triptych library). No random
    fill — a metadata-sparse scene simply yields a short list.
    """
    raw_id = scene_id[len("scene-"):] if scene_id.startswith("scene-") else scene_id
    target = await stash_client.get_scene(raw_id)
    if not target:
        logger.debug(f"Similar: target {raw_id} not found in Stash — empty")
        return []

    perf_ids = _cap_random(_ids(target.get("performers")), PERFORMER_CAP)
    tag_ids = _cap_random(_ids(target.get("tags")), TAG_CAP)
    studio_id = (target.get("studio") or {}).get("id")

    seeds = ([("performer", p) for p in perf_ids]
             + [("tag", t) for t in tag_ids]
             + ([("studio", studio_id)] if studio_id else []))
    if not seeds:
        logger.debug(f"Similar: target {raw_id} has no performers/studio/tags — empty")
        return []

    corpus, by_id, ps_hits, pt_hits = await _collect(seeds, vertical=vertical, unwatched_only=False)
    target_ids = {
        "performers": set(perf_ids),
        "tags": set(tag_ids),
        "studios": {studio_id} if studio_id else set(),
    }
    result = _rank(by_id, target_ids, corpus, ps_hits, pt_hits, exclude_ids={raw_id}, limit=limit)
    logger.debug(
        f"Similar: target {raw_id} vertical={vertical} — {len(seeds)} seed facet(s), "
        f"{len(by_id)} candidate(s) → returning {len(result)}"
    )
    return result


def _top_facets(counter: Counter, cap: int) -> list:
    """Most-frequent facet ids (watch-history signal), capped."""
    return [fid for fid, _n in counter.most_common(cap)]


async def build_next_up_pool(limit: int = 25, vertical: bool = False) -> list:
    """Discovery rail: scenes similar to what you've recently watched, best-match first.

    Aggregates performer/studio/tag facets across recent watch history (kept by
    frequency, capped), scores unwatched candidates with the shared facet model, then
    backfills with random unwatched scenes so the rail always fills to ``limit``.

    ``vertical`` (the row is under the Triptych library) constrains both the facet
    queries and the backfill to PORTRAIT, so every tile is a valid triptych center.
    """
    history = await stash_client.fetch_recent_watch_history(limit=50)
    watched_ids = {s["id"] for s in history if s.get("id")}

    perf_ct: Counter = Counter()
    studio_ct: Counter = Counter()
    tag_ct: Counter = Counter()
    for s in history:
        perf_ct.update(_ids(s.get("performers")))
        studio = s.get("studio") or {}
        if studio.get("id"):
            studio_ct.update([studio["id"]])
        tag_ct.update(_ids(s.get("tags")))

    perf_ids = _top_facets(perf_ct, NEXTUP_PERFORMER_CAP)
    studio_ids = _top_facets(studio_ct, NEXTUP_STUDIO_CAP)
    tag_ids = _top_facets(tag_ct, NEXTUP_TAG_CAP)

    seeds = ([("performer", p) for p in perf_ids]
             + [("studio", st) for st in studio_ids]
             + [("tag", t) for t in tag_ids])

    candidates: list = []
    scored_pool = 0  # unique unwatched candidates the facet queries surfaced (pre-truncation)
    if seeds:
        corpus, by_id, ps_hits, pt_hits = await _collect(seeds, vertical=vertical, unwatched_only=True)
        scored_pool = len(by_id)
        target_ids = {"performers": set(perf_ids), "tags": set(tag_ids), "studios": set(studio_ids)}
        candidates = _rank(by_id, target_ids, corpus, ps_hits, pt_hits,
                           exclude_ids=watched_ids, limit=limit)
    scored_count = len(candidates)  # how many came from affinity scoring (before backfill)

    # Backfill with global unwatched scenes so the rail is never short.
    if len(candidates) < limit:
        have = {c["id"] for c in candidates} | watched_ids
        shortfall = limit - len(candidates)
        backfill_filter = {"play_count": {"value": 0, "modifier": "EQUALS"}}
        if vertical:
            backfill_filter["orientation"] = {"value": ["PORTRAIT"]}
        backfill = await stash_client.fetch_scenes(
            filter_args={"sort": "date", "direction": "DESC"},
            page=1, per_page=shortfall + 10,
            scene_filter=backfill_filter,
        )
        for s in (backfill or {}).get("scenes", []):
            if len(candidates) >= limit:
                break
            if s["id"] not in have:
                candidates.append(s)
                have.add(s["id"])

    logger.debug(
        f"Next Up: {len(history)} watched → {len(seeds)} seed facet(s), "
        f"{scored_pool} scored candidate(s) → {scored_count} from scoring "
        f"+ {len(candidates) - scored_count} backfilled = {len(candidates)} (limit {limit})"
    )
    return candidates
