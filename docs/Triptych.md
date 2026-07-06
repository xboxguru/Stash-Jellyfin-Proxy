# Vertical Multi-View ("Triptych")

A specialized, toggleable library of vertical (portrait) videos. Pressing play composites the
selected clip (center) with two auto-selected looping side clips into a single 16:9 HLS stream,
audio from the center clip only. The same compositor also powers an optional always-on "Vertical
TV" Live TV channel that cycles fresh triptychs continuously.

This document covers the whole feature end to end: library filtering → side-clip selection →
compositor → hardware encoding → the Vertical TV channel. It's written for a developer picking
this up cold — the *why* behind each non-obvious choice matters as much as the *what*.

## Library and filtering

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

This module is deliberately tiny and framework-free: the side-clip selection algorithm and the
Vertical TV channel both reuse the same predicate for candidate filtering.

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

A scene played from the Vertical library composites (see below); the same scene played from a
normal library direct-plays like anything else — the trigger is the *library it was played
from*, not something intrinsic to the scene (see "The `vscene-` id namespace" below).

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

## Side-clip selection — `core/vertical_selection.py`

Picks the 2 looping side clips for a chosen center scene. It's pure selection logic — no
FFmpeg, no session state — so both `api/vertical_engine.py` (VOD compositor) and
`api/live_tv_engine.py` (Vertical TV channel) can call it without pulling in the playout
stack, and the algorithm is unit-testable in isolation.

### The weighting model

Candidates are drawn from a single vertical-only fetch (`stash_client.fetch_scenes`
with the same `orientation: PORTRAIT` filter as the library browse path, `per_page: -1`,
refined through `core.vertical.filter_vertical_scenes`). From that pool, four **category
pools** are built against the center scene:

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
set. This is what makes the "next-heaviest category" fallback happen for free: if slot 1
exhausted the only candidate in, say, Tags, slot 2 naturally re-normalizes over whatever's
left rather than needing special-cased retry logic.

### Fallback ordering, and why

1. **A category's pool is empty** → excluded from the weighted category draw entirely
   (steps above). Cheapest and most common case — e.g. a center scene with no studio set.
2. **Every category is empty, or every configured weight is 0** → uniform-random pick
   over all remaining eligible vertical scenes (`"uniform_random_all_categories_empty"`
   in the logs). This is the true "no signal to rank by" case — better to hand back
   *some* vertical clip than to fail the whole selection because metadata is sparse.
3. **No eligible candidates left at all** (`"no_eligible_candidates"`) → that slot picks
   nothing.
4. **Only 1 distinct eligible side existed across both slots** → the tiny-library
   rule: repeat that one clip for both slots rather than fail. A repeated side is a much
   smaller UX hit than not offering multi-view at all for a library that's still growing.
5. **No other vertical scenes exist besides the center** → `select_side_clips` returns
   `[]`. This is the one case selection *can't* paper over — the caller is expected to
   fall back to normal single-video playback (VOD) or retry after a delay (Vertical TV)
   and log a warning.

Every pick and fallback logs which path was taken (category name, or one of the fallback
labels above) so a thin library's behavior is diagnosable from the logs alone.

### Picking a center — `pick_center_and_sides()`

The VOD path always has an explicit center (the scene the user pressed play on). The
Vertical TV channel doesn't — it needs to invent one every round. `pick_center_and_sides
(exclude_ids=None)` fetches the same vertical candidate pool, picks a random scene as
center (excluding `exclude_ids` when that still leaves candidates — dropped rather than
failing the round if it wouldn't), and then calls `select_side_clips` on it exactly like
the VOD path. Returns `(center_scene, [left_id, right_id])`, or `None` if the library
can't support a triptych at all. One function, reused by both playout paths — the fallback
behavior above works identically whether the center was chosen by a user or by the channel
feeder.

## The VOD compositor — `api/vertical_engine.py` + `api/vertical_routes.py`

Turns a chosen center clip + its two selected sides into one 16:9 HLS stream. Deliberately
**reuses the Live TV playout spine** (`api/live_tv_engine.py`) rather than reinventing it: a
long-lived master FFmpeg process encoding a uniform 1920×1080 / yuv420p / 30 fps + s16le
48 kHz stereo raw stream, fed over a pipe backend (`_FifoPipeBackend` on Linux,
`_TcpRelayPipeBackend` on Windows), into an HLS ladder.

### Triggering: the `vscene-` id namespace

Jellyfin playback is context-free — `PlaybackInfo`/`stream` receive only an item id, and
the *same* scene has the same id in every library. To honor "multi-view fires only from the
Vertical library" without a parallel id scheme rippling through images/metadata/userdata/
streams, Vertical-library items are minted with a **`vscene-`** prefix instead of `scene-`
(`format_jellyfin_item(scene, vertical=True)`):

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
yuv420p 30 fps + s16le 48 kHz stereo) fed over the shared pipe backend, encoded once to
H.264/AAC. `-probesize 32 / -analyzeduration 0` on both inputs suppresses avformat's
stream probe — mandatory for the two-pipe design (probing input #0 reads only the video
socket while a single interleaving sub would block on audio → deadlock).

**Why two sub-processes, not one with two outputs:** raw 1080p30 video (~746 Mbps) and
raw PCM (~1.5 Mbps) have a ~500× bandwidth gap. In a single sub, the instant the master's
video buffer fills, that one process blocks on the video write and can no longer emit the
next audio packet either, starving the master's AAC encoder into a permanent deadlock
(documented and verified in `live_tv_engine._feed_one_scene`). Splitting video and audio
into separate processes gives each an independent backpressure path. The compositor keeps
this split exactly — and so does the Vertical TV channel (below).

On the TCP (Windows) backend the attach ordering is preserved: master claims the video
endpoint first, the composite sub connects second (producer), then the master attaches to
the audio endpoint (only possible after `find_stream_info()` on the video input completes,
which needs the composite sub already writing), then the audio sub connects.

### Filtergraph geometry (Shape A)

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

`build_composite_cmd()` and `build_audio_cmd()` (module-level functions in
`api/vertical_engine.py`) are the single source of truth for this filtergraph — both the
VOD `_VerticalSessionManager` and the Vertical TV channel feeder call them, so there is
exactly one place that knows the lane geometry.

("Shape B" — three separate lane pipes with the master doing the `hstack`, which would
enable live side swap-in — was considered and rejected as unnecessary complexity since
sides just loop; Shape A above is what shipped.)

### Center seek = full session relaunch

A center seek (client re-requesting the manifest with a different `StartTimeTicks`, or an
explicit `POST /vertical/{session}/seek`) is a **full session relaunch** with the new
`-ss` on the center input only — the sides just keep looping from their own start. The
manager reuses the session's cached sides on relaunch, so scrubbing never re-rolls them.
This is the deliberately simple approach; re-pointing only the center feeder is a later
optimization if scrubbing feels heavy. Backward seeks within the already-encoded range
work natively because the master uses `hls_playlist_type=event` (the full segment list is
retained and `EXT-X-ENDLIST` is written when the center ends), so a relaunch is only needed
to jump ahead of the live encode edge.

### Encoders, concurrency, idle teardown

- **Encoder:** selected by `VERTICAL_HWACCEL` (`none|nvenc|qsv|vaapi|auto`) through the shared
  probe (see § Hardware encoding below). The master's `-c:v` block (plus any device-init /
  `hwupload` filter) is supplied by the probed encoder. Decode + `hstack` still run on CPU.
- **Concurrency:** `VERTICAL_MAX_SESSIONS` (default 2). A launch that would exceed the cap
  is refused (logged), and the caller falls back to single-video playback. 3 decodes + 1
  encode is heavy — the cap is a safety rail (Stash is single-user).
- **Idle teardown:** `VERTICAL_IDLE_TIMEOUT` (default 60 s) — a 20 s watchdog reaps any
  session with no manifest/segment requests past the timeout, killing the subs + master,
  closing the pipe backend, and deleting the session temp dir.

### Logging

Per-session FFmpeg logs rotate at `{LOG_DIR}/vertical_ffmpeg/{session}.log` (same 10 MB
rotation as Live TV). The engine logs session lifecycle (center + sides + encoder +
backend + seek; teardown reason), full master/composite/audio commands at DEBUG, the
readiness gate, seek relaunches (old→new position), concurrency refusals, and the
single-video fallback with its reason.

## Hardware encoding — `core/hw_encoder.py`

`VERTICAL_HWACCEL` (`none|nvenc|qsv|vaapi|auto`) picks the master's H.264 **encoder**.
The selection logic is a small, framework-free, engine-agnostic helper in
`core/hw_encoder.py` (`resolve_h264_encoder`) so Live TV can adopt the identical
CPU/NVENC/QSV/VAAPI path later without duplicating it (DRY).

### Probe / fallback behavior

Being *compiled into* a build (jellyfin-ffmpeg carries all four) is not proof an encoder
will *initialize* — there may be no GPU, no `/dev/dri`, or no host driver. So selection is a
real **one-frame test-encode**, not an `-encoders` string match:

```
ffmpeg -hide_banner <device-init> -f lavfi -i color=black:320x240 -frames:v 1 \
       [-vf <hwupload>] -c:v <encoder> <rate-control> -f null -
```

Only an encoder that actually produced a frame (clean exit) is chosen. The resolution:

- **`none`** → `libx264`, no probe.
- **`auto`** → probe **NVENC → QSV → VAAPI** in order; take the first that passes, else CPU.
- **`nvenc` / `qsv` / `vaapi`** → that encoder if it probes OK, else a **logged CPU fallback**
  (an unavailable explicit choice never silently tries a *different* GPU encoder).
- unknown value → `libx264`.

The probe runs **once at startup** (main.py lifespan, only when `ENABLE_VERTICAL_MULTI`) so the
effective encoder is logged before the first play, and the result is cached per
`(mode, ffmpeg_bin)` — per-session launches never re-probe. Because the probe test-encodes
(and blocks), the engine runs it off the event loop via `run_in_executor`.

### Encoder-only (decode stays on CPU)

The 3-input decode, per-lane scale/crop, and `hstack` still run on the CPU; only the final
encode moves to the GPU. The raw `yuv420p` frames reaching the master are already in system
memory, so:

- **NVENC / libx264** ingest system frames directly — just a `-c:v` swap.
- **VAAPI / QSV** can't take system frames, so their `EncoderConfig` adds device-init before
  the inputs (`-vaapi_device …` / `-init_hw_device qsv=hw`) and an `hwupload` `-vf` to push the
  frames onto the GPU right before the encoder. `EncoderConfig` bundles those three arg groups
  (`input_args` / `vfilter` / `output_args`) so the master command just splices them in.

**Why not GPU decode/scale too:** full-GPU decode is what pulls the CUDA *runtime* (hundreds
of MB → GB in the image), and the *encode* is the CPU-heavy part most needed offloaded for
`3 decodes + 1 encode`. Encoder-only banks most of the CPU win at ~0 extra image size. GPU
decode/scale is a possible future optimization if the concurrency cap still feels tight.

### Image: why jellyfin-ffmpeg, and the size tradeoff

The Docker image is based on **jellyfin-ffmpeg7** (a `.deb` from the jellyfin-ffmpeg releases)
instead of the apt `ffmpeg` package, with `FFMPEG_PATH=/usr/lib/jellyfin-ffmpeg/ffmpeg`. One
maintained binary carries NVENC + QSV + VAAPI + AMF with the matching Intel drivers bundled —
purpose-built for exactly this transcoding workload, and far fewer "why won't QSV initialize"
problems than hand-assembling apt driver packages.

| Encoder | Added to image | Runtime requirement |
|---|---|---|
| NVENC (encode only) | ~0 MB (host driver injected) | NVIDIA Container Toolkit + `--gpus all` |
| Intel QSV / VAAPI | bundled in jellyfin-ffmpeg | `--device /dev/dri:/dev/dri` passthrough |
| CPU (`libx264`) | — | nothing (the automatic fallback) |
| **Net over the old apt-ffmpeg image** | **+150–300 MB** (mostly the Intel stack) | per-encoder, above |

NVENC rides the host driver essentially for free; the ~+150–300 MB is the one-time cost of the
bundled Intel stack. Because decode stays on CPU, we avoid pulling the CUDA runtime.

### Docker prerequisites (operator)

- **NVENC** — NVIDIA Container Toolkit + `--gpus all` + a host NVIDIA driver.
- **Intel QSV / VAAPI** — `--device /dev/dri:/dev/dri` passthrough.
- **CPU** — nothing; it's the automatic fallback when no GPU is available or the probe fails.

## Vertical TV channel — `api/live_tv_engine.py`

An optional always-on Live TV channel that runs the compositor continuously, cycling fresh
center/side clips forever instead of playing one chosen scene. It's a genuinely new *Live TV
channel type*, not a new engine: it reuses the Dynamic Stash Channels' FFmpeg manager
(`_FFmpegChannelManager`), the same PlaybackInfo/stream-serving code, and — critically — the
exact filtergraph the VOD compositor uses (`build_composite_cmd`/`build_audio_cmd` in
`api/vertical_engine.py`). There is no second compositor implementation anywhere.

### Why it fits as a channel, not a session

Live TV channels and VOD compositor sessions solve different problems that happen to share a
playout spine:

- A **VOD session** (`_VerticalSessionManager`) is keyed by `{scene_id}-{nonce}` and exists
  once per *play* — it ends when the center ends, sides are picked once and stay fixed for
  that play, and there's a concurrency cap per Jellyfin session.
- A **channel** (`_FFmpegChannelManager`) is keyed by channel id and exists once *total* —
  every viewer of "Vertical TV" shares the same running FFmpeg process, and it's expected to
  run indefinitely with no viewer-driven lifecycle beyond idle teardown.

Because the Vertical TV channel has no single "play" to key sessions by (it cycles rounds
forever), it belongs on the channel side of that split, sharing infrastructure with the Stash
tag/filter/shorts channels rather than the VOD manager.

### The channel is synthetic, not a channels.json entry

Tag/filter/shorts channels are user-created rows in `channels.json` (`_channels_config`),
each with a fixed scene lineup that the schedule builder turns into `_stash_schedule[tvg_id]`
EPG entries. Vertical TV has **no fixed lineup at all** — center and sides are picked live,
fresh, every round — so there is nothing for the channel-editor CRUD or the schedule builder
to store, edit, or reorder.

Instead, `api/live_tv_data.py`'s `_get_stash_channels()` appends one synthetic channel dict
(`_build_vertical_tv_channel()`, `stash_type: "vertical_tv"`) whenever
`_vertical_tv_enabled()` is true, right alongside the persisted tag/filter/shorts channels
from `channels.json`. It's registered into the same `_channel_info_map`/`_stash_channel_map`
lookups as every other channel, so `get_channel_by_jellyfin_id`, PlaybackInfo dispatch,
guide listings, and "now playing" all work for it without any new code path — they already
branch on `ch.get("stash_type")` being truthy, and `"vertical_tv"` satisfies that the same way
`"tag"`/`"filter"`/`"shorts"` do.

`_rebuild_stash_schedules` / `_run_maintenance_update` explicitly skip `vertical_tv` channels
(nothing to build), and `_build_stash_channel_playlist` short-circuits to `([], 0.0)` for them
— always "airing", seek always 0 since there's no meaningful position to resume into a channel
that never repeats the same content.

### Gating — `ENABLE_VERTICAL_TV_CHANNEL`

Three flags must all be true for the channel to appear:

| Flag | Why required |
|---|---|
| `ENABLE_STASH_CHANNELS` | Vertical TV reuses the Dynamic Stash Channels plumbing wholesale — PlaybackInfo, stream serving, the FFmpeg manager. |
| `ENABLE_VERTICAL_MULTI` | The compositor and side-selection algorithm are the Vertical Multi-View feature; the channel is just that feature run continuously. |
| `ENABLE_VERTICAL_TV_CHANNEL` | The channel-specific toggle. |

`ENABLE_LIVE_TV` (the master Live TV switch) still gates everything as usual —
`_live_tv_enabled()` now also returns true when Vertical TV alone is configured, so the
channel works even if a deployment has neither Tunarr nor plain Stash tag/filter channels
enabled.

`VERTICAL_TV_CHANNEL_NUMBER` (default 9000) sets its channel number; picked well above the
default Stash channel start number (5001) so operators using both don't have to think about
collisions.

### The feeder: `_feeder_vertical` / `_feed_one_vertical_round`

`_FFmpegChannelManager._feeder` is normally the scheduled-scene loop that walks
`_stash_schedule` one entry at a time. For a `vertical_tv` channel it dispatches instead to
`_feeder_vertical`, which loops forever:

1. `core.vertical_selection.pick_center_and_sides(exclude_ids=recent_centers)` — a fresh
   random center + 2 sides, excluding the last 5 centers played so the channel doesn't
   immediately repeat itself. If the vertical library can't support a triptych at all
   (`None`), it logs a warning and retries in 10 s rather than spinning.
2. Records the round in `_current_scene[cid]` (same shape the scheduled feeder uses) so the
   existing "now playing" endpoint (`endpoint_channel_now_playing`) works unmodified for this
   channel too.
3. `_feed_one_vertical_round(cid, center_id, sides, backend)` spawns exactly the two subs a
   VOD play would — a composite video sub (`build_composite_cmd`: 3 HTTP inputs, hstack, into
   the channel's video pipe) and a center-only audio sub (`build_audio_cmd`) — using the
   channel's already-established pipe backend and master (set up once in `_launch`, shared
   across every round the same way one master is shared across every scene in a normal
   channel). The round ends when the composite's own `-shortest` ends it (center finishes),
   then the loop picks a new round.

This mirrors `_feed_one_scene`'s spawn ordering exactly (video sub first, wait for the
master's audio-endpoint attach, then the audio sub) — the same TCP-backend handshake
constraint applies here as everywhere else in this file.

**Silent-center safety net:** unlike a VOD play — where a silent center just makes that one
session's audio track empty — a channel that stalls on a silent center stalls *every current
viewer* indefinitely, since the parent holds a keepalive writer FD on the audio pipe and the
master never sees EOF to react to. `_feed_one_vertical_round` carries the same fast-fail
pattern `_feed_one_scene` established for scheduled scenes: if the audio sub exits within 2 s
(no audio stream), it's replaced with an `lavfi` silence filler so the round still completes
in roughly the center's duration instead of hanging the channel.

**Master encoder:** the Vertical TV channel uses the Live TV master's existing CPU
(`libx264`) encode path unmodified — `VERTICAL_HWACCEL` only affects the VOD compositor's
master. Giving the channel GPU encoding too is a reasonable future addition (the channel
already reuses everything else), left out of this phase to keep the master's command
construction (shared by every Live TV channel type) untouched.

**Concurrency:** none needed beyond what already exists — like any Live TV channel, one
FFmpeg pipeline serves every simultaneous viewer of "Vertical TV", so there's no per-viewer
resource multiplication to cap. Running the VOD compositor and the Vertical TV channel at the
same time does add up on the host (each is a comparable "3 decodes + 1 encode" workload) —
worth watching if both are heavily used concurrently, but not coordinated between the two
managers in this phase.

**Idle teardown:** the channel is torn down by the same generic idle watchdog every Live TV
channel uses (`LIVE_TV_IDLE_TIMEOUT`), restarting a fresh round on the next play request.

### Tests

`tests/test_live_tv_vertical.py` — gating (`_vertical_tv_enabled`/`_live_tv_enabled`, all
three flags required), channel-list assembly (`_get_stash_channels` appends the synthetic
channel only when fully enabled, and never persists it to `channels.json`), the
`_build_stash_channel_playlist` short-circuit, feeder dispatch, `_feeder_vertical`'s
round-picking and "now playing" tracking, and `_feed_one_vertical_round`'s sub-spawning
(success, composite-spawn failure, audio-attach timeout, and the silence-filler fallback).
The FFmpeg manager is mocked — no real ffmpeg or subprocesses in tests, matching the VOD
compositor's own test approach. `tests/test_vertical_selection.py` covers
`pick_center_and_sides` (empty/single-scene library, center exclusion, and the
drop-exclusion-rather-than-fail fallback). `tests/test_config.py` covers the two new config
keys' coercion, env-override, and save/load round-trip.

## Config reference

All keys follow the "all four places" convention in `config.py` (defaults block,
`save_config()` `keys_to_save`, `_coerce_config_value()` type bucket, `_supported_keys`), and
are surfaced in the settings GUI (`templates/components/tab_settings.html`, Library tab →
"Vertical Multi-View" card and Live TV tab → "Dynamic Stash Channels" card).

| Key | Default | Type | Meaning |
|---|---|---|---|
| `ENABLE_VERTICAL_MULTI` | `false` | bool | Show the "Vertical Multi-View" home-screen tile |
| `VERTICAL_ASPECT_MIN` | `1.3` | float | Minimum height÷width to count as vertical |
| `VERTICAL_WEIGHT_PERFORMER` | `50` | int | Side-selection weight: shares a performer with center |
| `VERTICAL_WEIGHT_TAGS` | `25` | int | Side-selection weight: shares ≥1 tag with center |
| `VERTICAL_WEIGHT_STUDIO` | `15` | int | Side-selection weight: same studio as center |
| `VERTICAL_WEIGHT_DATE` | `10` | int | Side-selection weight: close in date to center |
| `VERTICAL_TAG_WINDOW` | `30` | int | Keep top-N tag-pool candidates ranked by shared-tag count |
| `VERTICAL_DATE_WINDOW_DAYS` | `30` | int | Date-proximity pool: ± this many days of center's date |
| `VERTICAL_IDLE_TIMEOUT` | `60` | int | Seconds of no VOD manifest/segment requests before tearing a session down |
| `VERTICAL_MAX_SESSIONS` | `2` | int | Concurrent VOD composite sessions; over cap → single-video fallback |
| `VERTICAL_HWACCEL` | `"auto"` | enum | VOD master encoder: `none/nvenc/qsv/vaapi/auto` |
| `ENABLE_VERTICAL_TV_CHANNEL` | `false` | bool | Enable the always-on Vertical TV Live TV channel |
| `VERTICAL_TV_CHANNEL_NUMBER` | `9000` | int | Channel number for Vertical TV |

`VERTICAL_ASPECT_MIN` is the codebase's first **float** config key — a float bucket was
added to `_coerce_config_value()`, and the settings form's numeric submit path
(`templates/components/scripts.html`) picks `parseFloat` over `parseInt` when a key's
`DEFAULTS` entry is non-integer (previously `parseInt("1.3")` would have silently saved `1`).
