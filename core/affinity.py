"""Pure scene-affinity primitives shared across features.

Both "similar scenes" (``core/similar.py``) and Triptych side-selection
(``core/vertical_selection.py``) rank candidates by the same idea: weighted
facet overlap with a target, where a facet shared by *fewer* scenes is a stronger
signal (rarity weighting). This module is the single, framework-free, I/O-free home
for that math so neither feature reimplements it (DRY).

The two features differ only in where the "corpus count" behind ``rarity`` comes
from: /similar uses each facet query's global Stash ``count``; Triptych uses the
facet's frequency *within the vertical candidate set* (so its seeded, deterministic
channel scheduling stays a pure function of the candidate list — no live counts).
Either way the scoring is identical.
"""
import math

# ── Tunable facet weights (shared defaults; callers may pass their own base) ─────
PERFORMER_BASE     = 50   # shared performer — the strongest affinity signal
TAG_BASE           = 30   # shared tag (per tag, rarity-weighted)
STUDIO_BASE        = 25   # same exact studio
PARENT_STUDIO_BASE = 6    # same studio *family* (flat; only when exact didn't connect)
PARENT_TAG_BASE    = 15   # same tag *family* (flat; only when exact didn't connect)


def rarity(count: int) -> float:
    """Inverse-log rarity multiplier: a facet on few scenes is a stronger signal
    than a generic one on hundreds. rarity(12) ≈ 0.74, rarity(700) ≈ 0.35."""
    return 1.0 / math.log10(max(int(count or 0), 0) + 10)


def ids(items) -> set:
    """Ids present in a list of {id, ...} dicts (studio/tag/performer fragments)."""
    return {i["id"] for i in (items or []) if i.get("id")}


def score_candidate(candidate: dict, target: dict, corpus: dict,
                    parent_studio_hits: set = frozenset(),
                    parent_tag_hits: set = frozenset()) -> float:
    """Weighted facet-overlap score of one candidate against the target facets.

    ``target`` carries pre-extracted id *sets* per facet type:
    ``{"performers": set, "tags": set, "studios": set}`` — a single-scene caller
    just passes a one-element ``studios`` set. Exact facets are rarity-weighted via
    ``corpus`` (``(facet_type, id) -> count``). Parent-tier credit is flat and
    applies only when the candidate did NOT already connect on that exact facet, so
    a scene in the same studio/tag family is never paid twice for it.
    """
    cid = candidate.get("id")
    score = 0.0

    for pid in target["performers"] & ids(candidate.get("performers")):
        score += PERFORMER_BASE * rarity(corpus.get(("performer", pid), 0))

    shared_tags = target["tags"] & ids(candidate.get("tags"))
    for tid in shared_tags:
        score += TAG_BASE * rarity(corpus.get(("tag", tid), 0))

    cand_studio = (candidate.get("studio") or {}).get("id")
    shares_studio = bool(cand_studio) and cand_studio in target["studios"]
    if shares_studio:
        score += STUDIO_BASE * rarity(corpus.get(("studio", cand_studio), 0))

    if not shares_studio and cid in parent_studio_hits:
        score += PARENT_STUDIO_BASE
    if not shared_tags and cid in parent_tag_hits:
        score += PARENT_TAG_BASE

    return score
