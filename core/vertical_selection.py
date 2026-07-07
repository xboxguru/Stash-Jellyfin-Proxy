"""Side-clip selection for Vertical Multi-View ("Triptych") — Feature 1, Phase 1.

Given a center scene, picks 2 side scenes to loop alongside it. Candidates are
drawn from weighted category pools (Performer / Tags / Studio / Date proximity),
built from the same vertical-only candidate set (server-side orientation filter
+ client-side aspect refinement from core.vertical — see docs/Triptych.md).

No FFmpeg or session concerns here — this module is pure selection logic so
api/vertical_engine.py (Phase 1 compositor) can call it without pulling in the
playout stack.
"""
import logging
import random
import datetime
import config
from core import stash_client
from core.vertical import filter_vertical_scenes, vdebug

logger = logging.getLogger(__name__)

# Category name -> the config attribute holding its weight (Shared Conventions:
# all four config places + GUI already added for these keys — see config.py).
_CATEGORY_WEIGHT_ATTRS = {
    "performer": "VERTICAL_WEIGHT_PERFORMER",
    "tags": "VERTICAL_WEIGHT_TAGS",
    "studio": "VERTICAL_WEIGHT_STUDIO",
    "date": "VERTICAL_WEIGHT_DATE",
}

_SIDE_SLOTS = 2


def _weighted_choice(pairs: list, rng: random.Random | None = None) -> object:
    """Picks one item from a list of (item, weight) pairs, weighted-random.

    If rng is provided, uses that seeded Random instance instead of module-level random.
    """
    _rng = rng if rng is not None else random
    items = [item for item, _ in pairs]
    weights = [weight for _, weight in pairs]
    return _rng.choices(items, weights=weights, k=1)[0]


def _resolve_date(scene: dict):
    """Scene date, falling back to created_at per §1.2.3. None if neither parses."""
    raw = scene.get("date") or scene.get("created_at")
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _shared_performer_pool(center: dict, candidates: list) -> list:
    """Uniform-weight pool: candidates sharing >=1 performer with center."""
    center_ids = {p.get("id") for p in (center.get("performers") or []) if p.get("id")}
    if not center_ids:
        return []
    matches = [
        s for s in candidates
        if center_ids & {p.get("id") for p in (s.get("performers") or []) if p.get("id")}
    ]
    return [(s, 1.0) for s in matches]


def _shared_studio_pool(center: dict, candidates: list) -> list:
    """Uniform-weight pool: candidates from the same studio as center."""
    studio_id = (center.get("studio") or {}).get("id")
    if not studio_id:
        return []
    matches = [s for s in candidates if (s.get("studio") or {}).get("id") == studio_id]
    return [(s, 1.0) for s in matches]


def _shared_tags_pool(center: dict, candidates: list, window: int) -> list:
    """Ranked-window pool: candidates sharing >=1 tag, weight = shared-tag count.

    Stash's BASE_SCENE_FIELDS only carries tag *names* (no ids), so overlap is
    computed by name. Keeps the top `window` by overlap — no minimum overlap,
    low-overlap clips just fall off naturally (§1.2.2).
    """
    center_tags = {t.get("name") for t in (center.get("tags") or []) if t.get("name")}
    if not center_tags:
        return []
    scored = []
    for s in candidates:
        overlap = len(center_tags & {t.get("name") for t in (s.get("tags") or []) if t.get("name")})
        if overlap > 0:
            scored.append((s, float(overlap)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:window]


def _date_proximity_pool(center: dict, candidates: list, window_days: int) -> list:
    """Ranked-window pool: candidates within ±window_days, weight favors closer dates."""
    center_date = _resolve_date(center)
    if center_date is None:
        return []
    scored = []
    for s in candidates:
        s_date = _resolve_date(s)
        if s_date is None:
            continue
        distance = abs((s_date - center_date).days)
        if distance <= window_days:
            scored.append((s, float(window_days + 1 - distance)))
    return scored


def _build_category_pools(center: dict, eligible: list) -> dict:
    """Non-empty category pools only — empty categories contribute nothing (§1.1)."""
    pools = {
        "performer": _shared_performer_pool(center, eligible),
        "tags": _shared_tags_pool(center, eligible, getattr(config, "VERTICAL_TAG_WINDOW", 30)),
        "studio": _shared_studio_pool(center, eligible),
        "date": _date_proximity_pool(center, eligible, getattr(config, "VERTICAL_DATE_WINDOW_DAYS", 30)),
    }
    return {name: pool for name, pool in pools.items() if pool}


def _pick_side(center: dict, candidates: list, excluded_ids: set, rng: random.Random | None = None) -> tuple:
    """Picks one side clip. Returns (scene_or_None, path_taken) for logging.

    Path: weighted category pick, re-normalized over non-empty pools only
    (§1.6.2-3). Falls back to uniform-random over all eligible vertical scenes
    when every category is empty or every configured weight is 0 (§1.6.4).

    If rng is provided, uses that seeded Random instance instead of module-level random.
    """
    eligible = [s for s in candidates if str(s.get("id")) not in excluded_ids]
    if not eligible:
        return None, "no_eligible_candidates"

    pools = _build_category_pools(center, eligible)
    weighted_categories = [
        (name, getattr(config, _CATEGORY_WEIGHT_ATTRS[name], 0))
        for name in pools
    ]
    weighted_categories = [(name, w) for name, w in weighted_categories if w > 0]
    vdebug(logger, (
        f"Vertical selection: center {center.get('id')} — eligible={len(eligible)}, "
        f"pools={{{', '.join(f'{n}: {len(p)}' for n, p in pools.items()) or 'none'}}}, "
        f"category weights in play={weighted_categories or 'none'}"
    ))

    if not weighted_categories:
        chosen = _weighted_choice([(s, 1.0) for s in eligible], rng=rng)
        return chosen, "uniform_random_all_categories_empty"

    category = _weighted_choice(weighted_categories, rng=rng)
    chosen = _weighted_choice(pools[category], rng=rng)
    return chosen, category


async def _fetch_vertical_candidates() -> list:
    """All vertical-library scenes: Stash orientation filter + core.vertical aspect refinement."""
    result = await stash_client.fetch_scenes(
        {}, per_page=-1, scene_filter={"orientation": {"value": ["PORTRAIT"]}}
    )
    return filter_vertical_scenes(result.get("scenes", []) if result else [])


async def select_side_clips(center_scene: dict) -> list:
    """Returns 2 side scene ids for center_scene, or [] to signal single-video fallback.

    - Enough distinct eligible vertical scenes: 2 unique sides, freshly re-rolled per play.
    - Exactly 1 distinct eligible side: repeats it for both slots (§1.2.5 tiny-library).
    - No other vertical scenes at all: [] — caller falls back to normal single-video
      playback and should log a warning (§1.2.5).
    """
    center_id = str(center_scene.get("id"))
    candidates = await _fetch_vertical_candidates()
    vdebug(logger, f"Vertical selection: center {center_id} — {len(candidates)} vertical candidate(s) fetched")
    excluded_ids = {center_id}
    sides = []

    for slot in range(1, _SIDE_SLOTS + 1):
        chosen, path = _pick_side(center_scene, candidates, excluded_ids)
        if chosen is None:
            logger.debug(f"Vertical selection: slot {slot} for center {center_id} has no eligible candidates")
            break
        side_id = str(chosen.get("id"))
        sides.append(side_id)
        excluded_ids.add(side_id)
        logger.info(f"Vertical selection: slot {slot} for center {center_id} -> scene {side_id} via '{path}'")

    if len(sides) == _SIDE_SLOTS:
        return sides
    if len(sides) == 1:
        logger.warning(
            f"Vertical selection: only 1 distinct eligible side for center {center_id} — repeating it for both slots"
        )
        return [sides[0], sides[0]]

    logger.warning(
        f"Vertical selection: no other vertical scenes besides center {center_id} — falling back to single-video playback"
    )
    return []


def select_side_clips_seeded(center_scene: dict, candidates: list, rng: random.Random) -> list:
    """Returns 2 side scene ids for center_scene, or [] to signal single-video fallback.
    Uses a seeded RNG for deterministic playback (triptych channel scheduling).

    - Enough distinct eligible vertical scenes: 2 unique sides, re-rolled deterministically.
    - Exactly 1 distinct eligible side: repeats it for both slots (§1.2.5 tiny-library).
    - No other vertical scenes at all: [] — caller falls back to normal single-video
      playback and should log a warning (§1.2.5).
    """
    center_id = str(center_scene.get("id"))
    vdebug(logger, f"Vertical selection: center {center_id} — {len(candidates)} vertical candidate(s) for seeded selection")
    excluded_ids = {center_id}
    sides = []

    for slot in range(1, _SIDE_SLOTS + 1):
        chosen, path = _pick_side(center_scene, candidates, excluded_ids, rng=rng)
        if chosen is None:
            logger.debug(f"Vertical selection: slot {slot} for center {center_id} has no eligible candidates")
            break
        side_id = str(chosen.get("id"))
        sides.append(side_id)
        excluded_ids.add(side_id)
        logger.info(f"Vertical selection: slot {slot} for center {center_id} -> scene {side_id} via '{path}' (seeded)")

    if len(sides) == _SIDE_SLOTS:
        return sides
    if len(sides) == 1:
        logger.warning(
            f"Vertical selection: only 1 distinct eligible side for center {center_id} — repeating it for both slots"
        )
        return [sides[0], sides[0]]

    logger.warning(
        f"Vertical selection: no other vertical scenes besides center {center_id} — falling back to single-video playback"
    )
    return []


async def pick_center_and_sides(exclude_ids: set | None = None) -> tuple:
    """Pick a random center scene plus its 2 sides — one full triptych round.

    Used by the Vertical TV Live TV channel (api/live_tv_engine.py) to cycle fresh
    triptychs continuously, the same way a human would keep pressing play on a new
    Vertical Multi-View clip. `exclude_ids` lets the caller avoid an immediate
    repeat of the last few centers played; if excluding everything would leave no
    candidates, the exclusion is dropped rather than failing the round.

    Returns (center_scene, [left_id, right_id]), or None if the vertical library
    doesn't have enough distinct scenes to compose a triptych at all.
    """
    candidates = await _fetch_vertical_candidates()
    if not candidates:
        return None
    pool = [s for s in candidates if str(s.get("id")) not in (exclude_ids or set())]
    exclusion_dropped = not pool
    if exclusion_dropped:
        pool = candidates
    vdebug(logger, (
        f"Vertical selection: picking center from {len(pool)} candidate(s) "
        f"({len(candidates)} total, {len(exclude_ids or ())} excluded"
        f"{'; exclusion dropped — it would leave no candidates' if exclusion_dropped else ''})"
    ))
    center = random.choice(pool)
    sides = await select_side_clips(center)
    if not sides:
        return None
    return center, sides


def pick_center_and_sides_seeded(candidates: list, center_id: str, rng: random.Random) -> tuple:
    """Pick a center scene (by id) plus its 2 deterministic sides — one full triptych round.

    Used by triptych channel scheduling (api/live_tv_engine.py) to resolve sides
    deterministically per schedule block. `center_id` must be in the candidates list.
    Uses the provided seeded RNG for deterministic playback.

    Returns (center_scene, [left_id, right_id]), or None if center_id not in candidates
    or there aren't enough distinct scenes for sides.
    """
    center = None
    for s in candidates:
        if str(s.get("id")) == str(center_id):
            center = s
            break
    if center is None:
        return None

    sides = select_side_clips_seeded(center, candidates, rng)
    if not sides:
        return None
    return center, sides
