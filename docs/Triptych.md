# Vertical Multi-View ("Triptych")

A specialized, toggleable library of vertical (portrait) videos. In later phases, pressing
play composites the selected clip (center) with two auto-selected looping side clips into a
single 16:9 HLS stream, with audio from the center clip only. The full feature plan lives in
`PLANNED_FEATURES.md` § Feature 1 (local planning doc, not tracked in git).

## Implementation Progress

| Phase | Scope | Status |
|---|---|---|
| **Phase 0** | Library + filtering: vertical predicate, config toggle, settings UI, home-screen tile, browse (scenes play normally) | ✅ Done |
| Phase 1 | VOD compositor (CPU): `vertical_engine` (Shape A), side-clip selection, PlaybackInfo transcode wiring, center seek, idle teardown | — |
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
