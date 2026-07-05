# Vertical Multi-View ("Triptych")

A specialized, toggleable library of vertical (portrait) videos. In later phases, pressing
play composites the selected clip (center) with two auto-selected looping side clips into a
single 16:9 HLS stream, with audio from the center clip only. The full feature plan lives in
`PLANNED_FEATURES.md` § Feature 1 (local planning doc, not tracked in git).

## Implementation Progress

| Phase | Scope | Status |
|---|---|---|
| **Phase 0** | Library + filtering: vertical predicate, config toggle, settings UI, home-screen tile, browse (scenes play normally) | ✅ Done |
| **Phase 1a** | Side-clip selection algorithm (pure logic, no FFmpeg) | ✅ Done |
| Phase 1b | VOD compositor (CPU): `vertical_engine` (Shape A), PlaybackInfo transcode wiring, center seek, idle teardown | — |
| Phase 1.5 | Hardware encoders (NVENC/QSV/VAAPI), jellyfin-ffmpeg base image, GPU decode/scale | — |
| Phase 2 | "Vertical TV" continuous Live TV channel reusing the compositor | — |

**Phase 0 orientation-filter decision:** hybrid. Stash's scene filter *can* express
orientation server-side (`orientation: {value: [PORTRAIT]}`, available since Stash v0.24,
Feb 2024) but has no aspect-ratio criterion — so the `height > width` half of the predicate
runs in Stash and the `height/width >= VERTICAL_ASPECT_MIN` half is refined client-side on
each fetched page. We did **not** over-fetch the whole library.

## How it works (Phase 0)

### The vertical predicate — `core/vertical.py`

A scene is "vertical" when its **primary file** (`scene["files"][0]`, the one playback uses)
satisfies:

```
height > width  AND  height / width >= VERTICAL_ASPECT_MIN   (default 1.3)
```

The 1.3 default keeps 9:16 phone portrait (~1.78) and 4:3 portrait (~1.33) while excluding
square and near-square files. Scenes with missing or zero dimensions are excluded — if we
can't prove a file is vertical, it doesn't belong in the library.

- `is_vertical_scene(scene, aspect_min=None)` — the predicate. `aspect_min` defaults to
  `config.VERTICAL_ASPECT_MIN` at call time so a settings change applies without restart.
- `filter_vertical_scenes(scenes)` — list refinement used by the browse path.

This module is deliberately tiny and framework-free: Phase 1's side-clip selection will
reuse the same predicate for candidate filtering.

### Library tile and browse routing

- `_get_libraries()` (`api/library_routes.py`) appends a **"Vertical Multi-View"** folder
  built from `encode_id("root", "vertical")`, gated on `ENABLE_VERTICAL_MULTI` — same
  pattern as every other feature-gated tile.
- Browsing that tile flows through the *generic* `_handle_library_browse` machinery. The
  only vertical-specific logic is:
  1. **`StashQueryBuilder.build()`** (`core/query_builder.py`): `decoded_parent_id ==
     "root-vertical"` adds `scene_filter["orientation"] = {"value": ["PORTRAIT"]}`. Because
     the filter is applied in the query builder, sorting, pagination, saved sync-level, and
     the lightweight alphabet-index cache all work unchanged.
  2. **`_handle_library_browse`**: when browsing `root-vertical`, each page of fully-fetched
     scenes passes through `filter_vertical_scenes()` before mapping to Jellyfin items
     (both the standard/random-sort path and the alphabet-bar path).

Playback is untouched in Phase 0 — a scene played from the Vertical library direct-plays
exactly like anywhere else. The compositor arrives in Phase 1.

### Why per-page client refinement (and its tradeoff)

Stash cannot express "aspect ratio ≥ X" in `SceneFilterType`, only the PORTRAIT/LANDSCAPE/
SQUARE orientation enum. Filtering the aspect ratio per fetched page keeps Stash-side
pagination (correct `page`/`per_page` math, no full-library over-fetch) at the cost of a
small inaccuracy: a portrait scene between 1.0 and `VERTICAL_ASPECT_MIN` (e.g. a 1.2:1
portrait) is dropped from its page *after* Stash counted it, so `TotalRecordCount` can
slightly overstate and that page renders a few items short. Such near-square portrait files
are rare in practice; if they ever matter, the fix is extending the lightweight index
(`stash_client.fetch_lightweight_index`) with `width`/`height` and paginating in Python the
way the alphabet-sort path already does.

**Older Stash caveat:** on Stash < v0.24 the `orientation` criterion doesn't exist, so
`findScenes` errors and the Vertical library renders empty (the GraphQL error is logged by
`stash_client`). No fallback is implemented — the proxy targets current Stash.

### Config

Both keys follow the "all four places" convention in `config.py` (defaults block,
`save_config()` `keys_to_save`, `_coerce_config_value()` type bucket, `_supported_keys`):

| Key | Default | Type | Meaning |
|---|---|---|---|
| `ENABLE_VERTICAL_MULTI` | `false` | bool | Show the "Vertical Multi-View" home-screen tile |
| `VERTICAL_ASPECT_MIN` | `1.3` | float | Minimum height÷width to count as vertical |

`VERTICAL_ASPECT_MIN` is the codebase's first **float** config key — a new float bucket was
added to `_coerce_config_value()`, and the settings form's numeric submit path
(`templates/components/scripts.html`) now picks `parseFloat` over `parseInt` when a key's
`DEFAULTS` entry is non-integer (previously `parseInt("1.3")` would have silently saved `1`).

### Settings GUI

`templates/components/tab_settings.html`, Library tab → "Vertical Multi-View" card: a
toggle for `ENABLE_VERTICAL_MULTI` and a `step="0.05"` number input for
`VERTICAL_ASPECT_MIN`. No custom JS needed — `/api/config` exposes every uppercase config
var, and the generic fetch/populate/save flow matches inputs by `name`.

### Tests

- `tests/test_vertical.py` — predicate boundaries (16:9, square, 1.2 portrait, exact
  threshold), config-driven vs. explicit `aspect_min`, missing/zero/None dimensions,
  primary-file-only judgment, and the list filter.
- `tests/test_config.py` — bool coercion for `ENABLE_VERTICAL_MULTI`, float coercion for
  `VERTICAL_ASPECT_MIN` (including invalid → `None`), and a full
  `save_config()` → `load_config_file()` round-trip preserving values and types.

## How it works (Phase 1a) — Side-clip selection

`core/vertical_selection.py` picks the 2 looping side clips for a chosen center scene.
It's pure selection logic — no FFmpeg, no session state — so `api/vertical_engine.py`
(Phase 1b) can call `select_side_clips(center_scene)` without pulling in the playout
stack, and the algorithm is unit-testable in isolation.

### The weighting model

Candidates are drawn from a single vertical-only fetch (`stash_client.fetch_scenes`
with the same `orientation: PORTRAIT` filter as the library browse path, `per_page: -1`,
refined through `core.vertical.filter_vertical_scenes` — see Phase 0 above). From that
pool, four **category pools** are built against the center scene:

| Category | Membership | Candidate weight (within pool) |
|---|---|---|
| Performer | shares ≥1 performer id with center | uniform (1.0) |
| Tags | shares ≥1 tag *name* with center (Stash's scene fields carry tag names, not ids) | shared-tag count — higher overlap is picked more often |
| Studio | same studio id as center | uniform (1.0) |
| Date | within `VERTICAL_DATE_WINDOW_DAYS` of center's `date` (falls back to `created_at`) | `window + 1 - day_distance` — closer dates are picked more often |

Only **non-empty** pools count. For each of the 2 side slots:
1. Pick a **category** by weighted-random over `VERTICAL_WEIGHT_PERFORMER/_TAGS/_STUDIO/_DATE`,
   re-normalized across whatever pools are currently non-empty (a category with 0 matches
   never gets picked — it isn't in the running at all, not picked-then-discarded).
2. Pick a **clip** within that category's pool, weighted-random by the per-candidate
   weight above (so Tags/Date favor the closest matches; Performer/Studio are a flat
   draw since there's no natural "how much" to rank by).
3. Tags additionally keep only the top `VERTICAL_TAG_WINDOW` candidates by shared-tag
   count before the weighted draw — an unbounded tag pool would let a handful of
   loosely-related clips (1 shared tag out of a large tag set) dilute the pick just as
   much as strongly-related ones.

Pools are **rebuilt from scratch for slot 2** with the slot-1 pick added to the exclusion
set. This is what makes the "next-heaviest category" fallback (§1.6.4) happen for free:
if slot 1 exhausted the only candidate in, say, Tags, slot 2 naturally re-normalizes over
whatever's left rather than needing special-cased retry logic.

### Fallback ordering, and why

1. **A category's pool is empty** → excluded from the weighted category draw entirely
   (steps above). Cheapest and most common case — e.g. a center scene with no studio set.
2. **Every category is empty, or every configured weight is 0** → uniform-random pick
   over all remaining eligible vertical scenes (`"uniform_random_all_categories_empty"`
   in the logs). This is the true "no signal to rank by" case — better to hand back
   *some* vertical clip than to fail the whole selection because metadata is sparse.
3. **No eligible candidates left at all** (`"no_eligible_candidates"`) → that slot picks
   nothing.
4. **Only 1 distinct eligible side existed across both slots** → §1.2.5's tiny-library
   rule: repeat that one clip for both slots rather than fail. A repeated side is a much
   smaller UX hit than not offering multi-view at all for a library that's still
   growing.
5. **No other vertical scenes exist besides the center** → `select_side_clips` returns
   `[]`. This is the one case selection *can't* paper over — the caller (Phase 1b) is
   expected to fall back to normal single-video playback and log a warning, per §1.2.5.

Every pick and fallback logs which path was taken (category name, or one of the fallback
labels above) so a thin library's behavior is diagnosable from the logs alone.

### Tests

`tests/test_vertical_selection.py` — each category pool builder (membership, weighting,
ranking, window truncation, boundary days); `_pick_side`'s category re-normalization
(verified by spying on `random.choices`' weights argument) and both fallback paths;
`select_side_clips` end-to-end for the 2-distinct-sides case, the tiny-library repeat,
the single-video empty-list case, and that non-vertical candidates returned by a raw
`fetch_scenes` result get filtered out before picking.
