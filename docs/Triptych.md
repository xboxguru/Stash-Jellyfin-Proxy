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
| **Phase 1b** | VOD compositor (CPU): `vertical_engine` (Shape A), PlaybackInfo transcode wiring, center seek, idle teardown | ✅ Done |
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

## How it works (Phase 1b) — Compositor

The VOD compositor turns a chosen center clip + its two selected sides into one
16:9 HLS stream. It lives in `api/vertical_engine.py` (`_VerticalSessionManager`)
and `api/vertical_routes.py` (the HTTP surface), and deliberately **reuses the Live
TV playout spine** rather than reinventing it.

### Triggering: the `vscene-` id namespace

Jellyfin playback is context-free — `PlaybackInfo`/`stream` receive only an item id,
and the *same* scene has the same id in every library. To honor "multi-view fires
**only** from the Vertical library" (decision 9) without a parallel id scheme rippling
through images/metadata/userdata/streams, Vertical-library items are minted with a
**`vscene-`** prefix instead of `scene-` (`format_jellyfin_item(scene, vertical=True)`):

- `jellyfin_mapper.decode_id()` transparently strips the leading `v` → `scene-11`, so
  **every existing consumer** (images, metadata, userdata, resume, subtitles, raw
  stream) works unchanged.
- `jellyfin_mapper.is_vertical_id()` is the one predicate that inspects the id *before*
  normalization; only the compositor-wiring spots call it (PlaybackInfo, `endpoint_stream`
  guard, item-details, MediaSources). The same clip browsed from a normal library keeps
  its `scene-` id and plays as a plain single video.

`_build_media_sources(..., vertical=True)` disables direct play and advertises an HLS
`TranscodingUrl`; `endpoint_item_details` re-derives the flag from the requested id so
the detail view keeps the compositor source.

### Playback wiring (mirrors Live TV)

1. `endpoint_playback_info` sees a `vscene-` id → `vertical_routes.vertical_playback_info`.
   It mints a **play-session id** `{scene_id}-{nonce}`, pre-warms the FFmpeg session
   (so segments exist by the client's first manifest fetch), and advertises a
   session-scoped `TranscodingUrl=/vertical/{session}/master.m3u8` (`SubProtocol=hls`,
   direct play off), `PlaySessionId={session}`. The source is a **finite** VOD
   (`RunTimeTicks` = center length, `IsLive=false`) so the client shows a scrub bar.
2. `endpoint_stream` has a guard mirroring the Live TV one: a `vscene-` id built straight
   into `/Videos/{id}/stream` (client bypassing PlaybackInfo) 302-redirects to a fresh
   composite session.
3. `/vertical/{session}/master.m3u8` and `/vertical/{session}/seg/{name}` serve the
   composite manifest (segment lines rewritten to absolute proxy URLs) and segments;
   `/seek` and `/stop` give explicit session control. The nonce is stable for a play, so
   every manifest/segment request within it hits the same session; a fresh play re-rolls
   the nonce (and therefore the sides).

### Pipe topology and the video/audio split

```
composite sub (3 HTTP inputs, hstack) ── v pipe ─▶┐
                                                  ├─▶ master FFmpeg ─▶ HLS ladder
center-audio sub (center clip only)  ── a pipe ─▶┘
```

The master is byte-for-byte the Live TV master: two raw pipes (rawvideo 1920×1080
yuv420p 30 fps + s16le 48 kHz stereo) fed over the shared pipe backend
(`_FifoPipeBackend` on Linux, `_TcpRelayPipeBackend` on Windows), encoded once to
H.264/AAC. `-probesize 32 / -analyzeduration 0` on both inputs suppresses avformat's
stream probe — mandatory for the two-pipe design (probing input #0 reads only the video
socket while a single interleaving sub would block on audio → deadlock).

**Why two sub-processes, not one with two outputs:** raw 1080p30 video (~746 Mbps) and
raw PCM (~1.5 Mbps) have a ~500× bandwidth gap. In a single sub, the instant the master's
video buffer fills, that one process blocks on the video write and can no longer emit the
next audio packet either, starving the master's AAC encoder into a permanent deadlock
(documented and verified in `live_tv_engine._feed_one_scene`). Splitting video and audio
into separate processes gives each an independent backpressure path. The compositor keeps
this split exactly.

On the TCP (Windows) backend the attach ordering is preserved: master claims the video
endpoint first, the composite sub connects second (producer), then the master attaches to
the audio endpoint (only possible after `find_stream_info()` on the video input completes,
which needs the composite sub already writing), then the audio sub connects.

### Filtergraph geometry (Shape A, §1.5)

```
-re -stream_loop -1 -i <left>        # side, loops forever
-re [-ss S]         -i <center>      # the clock; -ss applies here only
-re -stream_loop -1 -i <right>       # side, loops forever
-filter_complex
  [0:v]scale=-2:1080,crop=608:1080,setsar=1[l];
  [1:v]scale=-2:1080,crop=608:1080,setsar=1[c];
  [2:v]scale=-2:1080,crop=608:1080,setsar=1[r];
  [l][c][r]hstack=inputs=3,pad=1920:1080:(ow-iw)/2:0:black,fps=30,format=yuv420p[v]
-map "[v]" -shortest -f rawvideo <v pipe>
```

Each 1080-tall lane is cropped to **608×1080**; `hstack` → 1824×1080; `pad` centers to
exactly 1920×1080. Sides loop infinitely; `-shortest` ends the composite when the finite
center stream ends. Audio is the center clip only, normalized with the same
`aresample/aformat` chain as Live TV (`-map 0:a:0?` so a silent center doesn't fail).
Inputs are read with `-re` so the client can never outrun the encoder.

### Center seek = full session relaunch

A center seek (client re-requesting the manifest with a different `StartTimeTicks`, or an
explicit `POST /vertical/{session}/seek`) is a **full session relaunch** with the new
`-ss` on the center input only — the sides just keep looping from their own start. The
manager reuses the session's cached sides on relaunch, so scrubbing never re-rolls them.
This is Phase 1's deliberately simple approach (§1.5); re-pointing only the center feeder
is a later optimization if scrubbing feels heavy. Backward seeks within the
already-encoded range work natively because the master uses `hls_playlist_type=event`
(the full segment list is retained and `EXT-X-ENDLIST` is written when the center ends),
so a relaunch is only needed to jump ahead of the live encode edge.

### Encoders, concurrency, idle teardown

- **Encoder:** Phase 1b is **CPU-only** (`libx264`). `VERTICAL_HWACCEL`
  (`none|nvenc|qsv|vaapi|auto`) is surfaced now but every value resolves to libx264 and is
  logged as such; the NVENC/QSV/VAAPI paths + jellyfin-ffmpeg base image land in Phase 1.5.
- **Concurrency:** `VERTICAL_MAX_SESSIONS` (default 2). A launch that would exceed the cap
  is refused (logged), and the caller falls back to single-video playback. 3 decodes + 1
  encode is heavy — the cap is a safety rail (Stash is single-user).
- **Idle teardown:** `VERTICAL_IDLE_TIMEOUT` (default 60 s) — a 20 s watchdog reaps any
  session with no manifest/segment requests past the timeout, killing the subs + master,
  closing the pipe backend, and deleting the session temp dir.

### Config (all four places + GUI)

| Key | Default | Type | Meaning |
|---|---|---|---|
| `VERTICAL_IDLE_TIMEOUT` | `60` | int | Seconds of no requests before a session is torn down |
| `VERTICAL_MAX_SESSIONS` | `2` | int | Concurrent composites; over cap → single-video fallback |
| `VERTICAL_HWACCEL` | `auto` | enum | `none/nvenc/qsv/vaapi/auto` — CPU-only until Phase 1.5 |

Surfaced in the settings GUI under the "Vertical Multi-View" card → **Compositor**
(`tab_settings.html`); the three keys are in `DEFAULTS` (`scripts.html`) so the generic
save flow types them correctly.

### Logging (§1.8)

Per-session FFmpeg logs rotate at `{LOG_DIR}/vertical_ffmpeg/{session}.log` (same 10 MB
rotation as Live TV). The engine logs session lifecycle (center + sides + encoder +
backend + seek; teardown reason), full master/composite/audio commands at DEBUG, the
readiness gate, seek relaunches (old→new position), concurrency refusals, and the
single-video fallback with its reason.

### Tests

- `tests/test_stream_routes.py` — PlaybackInfo advertises the HLS compositor transcode
  (direct play off, `/vertical/…/master.m3u8`, `{scene}-{nonce}` PlaySessionId) for a
  `vscene-` item; single-video fallback when the compositor is unavailable; a disabled
  feature flag plays normally; and the `endpoint_stream` guard 302-redirects a
  `vscene-` stream URL to a composite session. (The FFmpeg manager is mocked — no real
  sub-processes in tests.)
- `tests/test_config.py` — int coercion for the two numeric keys, enum coercion +
  invalid→`auto` for `VERTICAL_HWACCEL`, and a `save_config()`→`load_config_file()`
  round-trip preserving values and types.

### Watch-items

- Client seek behavior through a custom HLS transcode is client-dependent; the
  `StartTimeTicks`-triggered relaunch + `event` playlist is the mechanism, but real
  scrubbing/resume should be verified against Wholphin/ExoPlayer on a device.
- Full relaunch on every scrub may feel heavy — optimize to re-point only the center
  feeder later if needed.
