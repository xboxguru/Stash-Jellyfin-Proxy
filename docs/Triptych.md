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
3. `/vertical/{session}/master.m3u8` serves a **proxy-synthesized VOD playlist** covering
   the entire center duration (not FFmpeg's own playlist — see § Full-length seek + segment
   cache); `/vertical/{session}/seg/{name}` serves each segment, producing it on demand if
   it isn't already cached. `/seek` and `/stop` give explicit session control. The nonce is
   stable for a play, so every manifest/segment request within it hits the same session; a
   fresh play re-rolls the nonce (and therefore the sides).

**Auth carve-out (resolved):** the manifest/segment fetch is issued by the player's HLS
stack directly against the `TranscodingUrl`, headerless — it carries no `api_key` query
param and no `X-Emby-Token`/`Authorization` header, so it can only ever pass the
IP-based auth path in `api/middleware.py::_is_image_or_video_authorized`, never the
strict `PROXY_API_KEY` check. That method originally recognized `/images/`, `/videos/`,
and the Live TV `/livetv/channels/...stream|seg|tunarr-relay` paths as media eligible for
IP auth, but not `/vertical/`, so the request 401'd even though the compositor itself was
healthy (session pre-warmed, segments on disk, FFmpeg procs running) — Live TV worked
only because its manifest lives under the already-allowlisted `/livetv/channels/` prefix.
The fix mirrors that branch: `_is_image_or_video_authorized` now also matches
`/vertical/{session}/master.m3u8` and `/vertical/{session}/seg/{name}` specifically —
**not** a blanket `/vertical/` prefix match, since `/seek` and `/stop` are mutating
session-control endpoints that must keep requiring the full API-key check. See
`tests/test_middleware.py` for the authorized-IP-passes / unknown-IP-still-401s cases
for both the manifest and segment paths, and the seek/stop non-carve-out.

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
[pace] -stream_loop -1 [-ss Ls] -i <left>    # side, loops forever; -ss phases it (see below)
[pace]              [-ss S]      -i <center>  # the clock; center -ss = seek position
[pace] -stream_loop -1 [-ss Rs] -i <right>   # side, loops forever; -ss phases it
-filter_complex
  [0:v]scale=608:1080:force_original_aspect_ratio=increase:force_divisible_by=2,crop=608:1080,setsar=1[l];
  [1:v]scale=608:1080:force_original_aspect_ratio=increase:force_divisible_by=2,crop=608:1080,setsar=1[c];
  [2:v]scale=608:1080:force_original_aspect_ratio=increase:force_divisible_by=2,crop=608:1080,setsar=1[r];
  [l][c][r]hstack=inputs=3,pad=1920:1080:(ow-iw)/2:0:black,fps=30,format=yuv420p[v]
-map "[v]" -shortest -f rawvideo <v pipe>
```

Each lane **scales to cover** the 608×1080 cell (`force_original_aspect_ratio=increase`)
then centre-crops to exactly **608×1080** — a plain `scale=-2:1080` would leave a source
narrower than 608/1080 (≈0.563, i.e. taller than 9:16 — e.g. a 720×1282 clip → 606 px)
narrower than the crop, and `crop=608:1080` then aborts the whole composite ("Invalid too
big size for width 608"). `hstack` → 1824×1080; `pad` centers to exactly 1920×1080. Sides
loop infinitely; the composite is bounded by an explicit `-t (center_duration − seek)` so it
exits at center EOF — `-shortest` alone is a **no-op** here (the filtergraph has a single
output stream), so without `-t` the composite hangs on the ended center input, freezing a
couple frames short of the final segment (which then never finalizes). Audio is the center
clip only, normalized with the same `aresample/aformat` chain as Live TV (`-map 0:a:0?` so a
silent center doesn't fail), bounded by the same `-t`.

`[pace]` is the per-input pacing token. The **VOD compositor runs full-speed** (no `-re`,
optionally `-readrate VERTICAL_READRATE`) — the client reads static files off disk and the
encoder outruns it, so there is no client-paced backpressure to preserve (see § Full-length
seek + segment cache). The **Vertical TV channel keeps `-re`** (realtime pacing) because a
channel is genuinely live. The side `-ss` offsets (`Ls`/`Rs`) phase the looping sides to the
timeline on a relaunch (§ Full-length seek).

`build_composite_cmd()` and `build_audio_cmd()` (module-level functions in
`api/vertical_engine.py`) are the single source of truth for this filtergraph — both the
VOD `_VerticalSessionManager` and the Vertical TV channel feeder call them, so there is
exactly one place that knows the lane geometry. `pace_args` defaults to `("-re",)`, so the
Vertical TV channel's call (which omits it) is byte-identical to before; the VOD path passes
the readrate-derived tokens instead.

("Shape B" — three separate lane pipes with the master doing the `hstack`, which would
enable live side swap-in — was considered and rejected as unnecessary complexity since
sides just loop; Shape A above is what shipped.)

### Full-length seek + segment cache

> **Status: implemented 2026-07-06, not yet field-tested.** Remove this line once VOD
> playback, full-range seeking, post-reap resume, and backfill have been verified on a
> real client.

The whole center timeline is seekable the instant playback starts, and spin-up is fast,
because of five interlocking pieces:

**1. Synthetic VOD playlist (proxy-owned).** `/vertical/{session}/master.m3u8` does *not*
relay FFmpeg's playlist. The proxy synthesizes a complete `#EXT-X-PLAYLIST-TYPE:VOD`
playlist (`_build_vod_playlist` in `api/vertical_routes.py`) covering the *entire* center
duration with uniform 4 s segments (the final segment declares its true, shorter length),
ended by `EXT-X-ENDLIST`. ExoPlayer derives its seekable range from the playlist, not the
advertised `RunTimeTicks`, so the full timeline is seekable from the first fetch. The
segment↔time contract is exact: the master forces keyframes with
`force_key_frames expr:gte(t,n_forced*4)` and cuts at `-hls_time 4`, so segment *N* always
covers center time `[N*4, N*4+4)`. `SEG_DURATION`, `hls_time`, and that expression must stay
in lockstep.

**2. Full-speed encode.** The vertical subs drop `-re` (the Live TV pacing flag) — the client
reads static files off disk, the encoder outruns it, and there is no client-paced
backpressure in the chain. The video/audio process split stays (that is the actual deadlock
protection; see § Pipe topology). With `-shortest` against the looping sides, the pipeline
exits on its own once the center is fully encoded, and the session degrades to pure static
files. `VERTICAL_READRATE` (float, default `0` = unlimited) optionally caps the burst via
`-readrate N` for thermally-constrained hosts.

**3. Segment range tracker + `ensure_segment(index)` — the single recovery path.** The
session tracks which segment indexes exist on disk (produced ranges; seeks leave holes).
A request for a missing segment calls `ensure_segment(index)`, which either **waits** (the
live encode head is within `_SEG_WAIT_LOOKAHEAD` segments — a momentary client-ahead-of-
encoder miss resolves in well under a second at full speed) or **relaunches** the subs with
`-ss index*4` on the center and `-start_number index` on the master, into the *same* session
dir (old segments stay valid). That one path serves **seek past the encode head**, **seek
back into an unfilled gap**, and **resume after a process reap** identically. A respin never
counts against `VERTICAL_MAX_SESSIONS` — it's the same session. Relaunches are debounced by
the session lock: a newer target re-decides after the previous respin lands rather than
stacking a second one. A request that carries **no** position (`None` vs `0.0` through
`_seek_seconds` → `ensure`) is steady-state and never disturbs the running encode.

**4. Sides are phased to the timeline, not to watch time.** Cached segments bake the side
pixels in: the segment at position *T* necessarily shows the sides at `T mod side_duration`
(the phase of a continuous start-to-finish encode). Relaunches therefore start each side at
`seek_seconds mod side_duration` (`_phase`, using side durations fetched once at launch) so
re-encoded content is byte-compatible with what the continuous encode would have produced —
cache-coherent and deterministic, and identical timeline positions always render the same.

**5. Background gap backfill.** When a run reaches the center end and the session hasn't been
explicitly stopped or reaped, `_schedule_backfill` relaunches at the earliest gap (created by
forward seeks) and lets it run to the end, so backward seeking always cache-hits eventually.
Backfill runs one relaunch at a time, defers to any live user-driven run (`ensure_segment`
wins), stops on a stall (a gap that made no progress), and is skipped after a stage-1 reap.

### Encoders, concurrency, two-stage teardown

- **Encoder:** selected by `VERTICAL_HWACCEL` (`none|nvenc|qsv|vaapi|auto`) through the shared
  probe (see § Hardware encoding below). The master's `-c:v` block (plus any device-init /
  `hwupload` filter) is supplied by the probed encoder. Decode + `hstack` still run on CPU.
- **Concurrency:** `VERTICAL_MAX_SESSIONS` (default 2). A launch that would exceed the cap
  is refused (logged), and the caller falls back to single-video playback. A session holds
  its slot from launch until stage-2 destroy (so a reaped-but-cached session still counts);
  a respin is the *same* session and never consumes a second slot. 3 decodes + 1 encode is
  heavy — the cap is a safety rail (Stash is single-user).
- **Disk guardrail:** a session renders the *full* center clip (~1–2 GB per 30 min at
  1080p30). Before launch the engine checks free space on the HLS temp volume and refuses
  below a 2 GB floor (falling back to single-video playback, logged).
- **Two-stage teardown:** a 20 s watchdog runs two clocks off the last fetch.
  **Stage 1 — process reap** at `VERTICAL_IDLE_TIMEOUT` (default 60 s): kill the subs +
  master, close the pipe backend, but **keep the cached segments** (a no-op if the encode
  already finished). **Stage 2 — session destroy** at `VERTICAL_SESSION_TTL` (default
  1800 s), or an explicit `/stop`: delete the temp dir and free the cap slot. A segment fetch
  is the liveness signal and resets both clocks; a client that unpauses after a reap
  transparently respins via `ensure_segment`. Expected first-frame latency with `-re` gone is
  ~3–5 s on the reference hardware (vs 15–20 s under realtime pacing) — the readiness gate
  waits for `VERTICAL_READY_SEGMENTS` (default 2) segment files.

### Logging

Per-session FFmpeg logs rotate at `{LOG_DIR}/vertical_ffmpeg/{session}.log` (same 10 MB
rotation as Live TV). The file spans the whole session (a session can span multiple runs —
initial encode, seek respins, backfill): a `===== FFmpeg session start … =====` banner, then
per run a `----- run start start_index=N … -----` marker with that run's master/composite/
audio commands, the three processes' stderr (`[composite]` / `[audio]` prefixes; master
unprefixed), and a `----- run end (reason) -----` marker, closed by `===== FFmpeg session
end (reason) =====` at stage-2 destroy.
The composite/audio sub commands carry the Stash `apikey` in their HTTP input URL
(`/scene/{id}/stream?apikey=...`); `core.vertical.redact_apikey()` masks it before any
command line is logged (proxy log and per-session file alike), and the Live TV feeder's
own scene-sub command logging uses the same helper — so a shared log file is safe to hand
to someone else for debugging without leaking the Stash key.
The engine logs session lifecycle at INFO: launch (center + sides + side durations + encoder
+ backend + total segments), the readiness gate, **every relaunch** with its trigger
(`back-seek/gap` / `resume/gap` / `seek-past-head`), old→new `start_index`, and the produced
range summary, **backfill** start/finish, **stage-1 process reap vs stage-2 destroy** each
with reason, concurrency refusals (with the ids of the sessions holding the slots), the
disk-guardrail refusal (with free space vs floor), and the single-video fallback with its
reason. A session that fails the readiness gate is torn down immediately rather than left
encoding orphaned.

Stderr draining splits on `\r` as well as `\n`: FFmpeg's periodic progress line is
`\r`-terminated on a pipe, and newline-only reading would grow one "line" until the
StreamReader limit killed the drain task — after which the OS stderr pipe fills and
FFmpeg itself blocks (see `_iter_stderr_lines` in `api/live_tv_engine.py`, shared with
Live TV).

Diagnostics beyond the lifecycle INFO lines (selection pool sizes and weights, full
FFmpeg commands, pipe-backend attach steps, per-request session decisions) go through
`core.vertical.vdebug()`: DEBUG normally, **promoted to INFO when `VERTICAL_DEBUG` is
on** — see § Debugging / logs below for how to flip it and why it's a config flag
rather than a logger level.

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

## Approved rework A: engine stability fixes

> **Status: approved design, NOT yet implemented.** Root causes below were confirmed from
> the 2026-07-06 channel field test (proxy log + `livetv_ffmpeg/63682d76...log`). The
> implementing chat must fold each fix into the relevant sections of this doc and delete
> this banner. **Implement this rework before rework B** — B builds on a stable engine.

Four confirmed defects, ordered by severity. Fixes 1–3 live in `api/live_tv_engine.py`;
fix 4 is in the shared builder in `api/vertical_engine.py` and benefits VOD too.

1. **Channel pipeline runs below realtime → clients exhaust the segment window and
   crash.** The round-#1 composite held `speed=0.58–0.61x / 18 fps` for its entire life:
   3 CPU decodes + hstack + the Live TV master's CPU `libx264` encode outrun this host.
   A viewer consumes at 1.0×, drains the `hls_list_size 15` (~60 s) window, hits the live
   edge, and the player dies; reconnecting replays the frozen window and dies at the same
   spot. **Fix:** extend the `core/hw_encoder.py` probe path to the *Live TV master*
   command (today `VERTICAL_HWACCEL` only wires into the VOD master — the doc's "future
   addition" is now load-bearing). All channel types benefit. Config: reuse the existing
   probe/fallback semantics; log chosen vs effective encoder per channel launch.
2. **Rounds never self-terminate — `-shortest` is a no-op on the composite. ✅ FIXED (VOD).**
   `-shortest` compares *output* streams and the composite has exactly one, so at center
   EOF `hstack` stalls waiting on the ended input: round #1 froze at frame 3524
   (~117.7 s ≈ center end) and hung ~85 s until externally killed (rc=1). On the VOD path
   this froze a couple frames short of the final segment, so a short clip's readiness gate
   never completed (seg1 never finalized). **Fix (shipped for VOD):** `build_composite_cmd`
   / `build_audio_cmd` take a `duration` arg emitting `-t`, and `_start_run` passes
   `center_duration − seek` (relaunches get the remaining-from-seek bound too). **Still TODO
   for the Vertical TV channel** — `_feed_one_vertical_round` must thread the center duration
   into the builders (it omits `duration` today, so the round still relies on the no-op
   `-shortest`); this is also a prerequisite for rework B's deterministic EPG.
3. **A dying sub kills the master, and the feeder never notices — the channel wedges
   permanently.** When round #1's composite died, the TCP relay's sub-forward ended
   (`WinError 64`), both master connections closed, and the master exited. Every later
   round's subs then died in <1 s with `-10053 WSAECONNABORTED` (rounds #2–#5…), while
   the feeder looped forever spawning corpses and the on-disk playlist stayed frozen —
   only a manual process kill recovered it. **Fixes:** (a) the feeder health-checks the
   master process before each round and on sub failure; master dead → tear down and
   relaunch the whole channel (bounded retries + backoff, loudly logged); (b) the pipe
   backend must survive an unclean sub abort without dropping the master-side connection
   (drop partial raw frames — a partial 3,110,400-byte frame shifts alignment and corrupts
   the master's rawvideo input); (c) the silent-center detector must distinguish "audio
   sub exited because the center has no audio stream" from "audio sub exited because the
   pipe endpoint aborted" before spawning the silence filler (rounds #2+ misdiagnosed
   this every time).
4. **Ultra-tall verticals crash the composite lane — VOD and channel alike. ✅ FIXED.**
   Scene 905 / scene 25527 (720×1282): `[Parsed_crop] Invalid too big or non positive size
   for width '608'`. The lane chain `scale=-2:1080,crop=608:1080` produced width < 608 for
   any source taller than 1080/608 ≈ 1.776:1 (e.g. 1080×2340 → 498×1080), which the vertical
   predicate (aspect ≥ 1.3) happily admits; the composite aborted before its first frame, so
   the master never finished probing input #0 and never attached to the audio endpoint —
   surfacing as a "master never attached to audio endpoint" timeout + retry loop at ~0 % CPU
   (not a relay/read-rate issue, as first suspected). **Fix (shipped):** cover-crop in
   `build_composite_cmd` — `scale=608:1080:force_original_aspect_ratio=increase:force_divisible_by=2,crop=608:1080,setsar=1`
   — scale to cover the lane, then centre-crop. One change, both consumers fixed. Covered by
   a unit test with an ultra-tall (720×1282 / 1080×2340) lane and a near-square 1.3:1 lane.

**Invariants:** channel `pace_args` stays `("-re",)` (live channels must not outrun wall
clock — only the VOD manager passes readrate tokens); the video/audio sub split is
untouched; the VOD full-length-seek rework's behavior is untouched.

## Approved rework B: Triptych channels (per-channel toggle)

> **Status: approved design, NOT yet implemented. Requires rework A first.** Supersedes
> § *Vertical TV channel* below (the synthetic-channel design) — the implementing chat
> must rewrite that section as implemented behavior and delete this banner.

The 2026-07-06 field test surfaced three *designed-in* gaps of the synthetic channel: no
Guide data (schedule builder skips `vertical_tv`), "channel doesn't exist" from the
rebuild endpoint (not a `channels.json` row), and a channel editor showing tags/filters
that do nothing. Rather than patching the special case, triptych becomes a **property of
ordinary channels** — deleting the special case deletes the whole bug class.

### Decisions (locked 2026-07-06)

1. **`triptych: true` is a per-channel flag in `channels.json`.** Any tag/filter/shorts
   channel can set it. Semantics: the channel's scene lineup is additionally filtered by
   the vertical predicate, and the feeder plays every block as a composite round
   (center + 2 sides) instead of a single scene. Uniform rule — a triptych channel is
   *all* triptych; there is no per-block mixed mode (considered, deferred: it complicates
   the feeder dispatch and makes EPG blocks ambiguous). To offer both flavors of the same
   content (e.g. standard shorts *and* triptych shorts), create two channels — the
   implementer must ensure shorts-type channels are instantiable like tag/filter channels
   if they are currently a singleton.
2. **The synthetic `vertical_tv` channel is deleted, with migration.** On startup, if
   `ENABLE_VERTICAL_TV_CHANNEL` is true and no migrated channel exists yet, create a real
   `channels.json` entry — name "Vertical TV", `triptych: true`, an all-verticals filter,
   channel number `VERTICAL_TV_CHANNEL_NUMBER` — then mark the migration done (one-shot;
   the user can freely edit or delete the real channel afterwards). The
   `_build_vertical_tv_channel` / `_feeder_vertical` special cases, the
   `_rebuild_stash_schedules` skip-branch, and the `_build_stash_channel_playlist`
   short-circuit are all removed. Retire `ENABLE_VERTICAL_TV_CHANNEL` +
   `VERTICAL_TV_CHANNEL_NUMBER` from config defaults after migration support ships
   (keep reading them for the migration itself).
3. **Deterministic schedule, exactly like other channels.** The lineup builder produces
   the center sequence from the channel's (vertical-scoped) query; the schedule builder
   turns it into `_stash_schedule` EPG entries with block duration = center duration
   (exact, thanks to rework A's `-t` bounds). Guide data, the rebuild endpoint, "now
   playing", and mid-block seek-on-join all work because it *is* a normal scheduled
   channel.
4. **Deterministic sides via seeded RNG, with a per-channel salt.** Side selection for a
   block runs the existing `core/vertical_selection.py` algorithm with a seeded
   `random.Random` instance instead of the module-level RNG:
   `seed = hash((channel_id, TRIPTYCH_SALT, schedule_generation, block_index, center_id))`.
   The salt is a free-text field on the channel config (default `""`): changing it
   re-rolls every block's sides without rebuilding the lineup — a cheap "shuffle the
   sides" knob. Determinism means a channel relaunch (rework A fix 3) or a mid-block
   viewer join reproduces the identical triptych.
5. **Mid-block join phases all three lanes.** Joining a block at offset *t* seeks the
   center by *t* (the scheduled feeder's existing seek math) and launches the sides at
   `t mod side_duration` — the builders already take per-side seeks
   (`build_composite_cmd(..., left_seek, right_seek)`), added by the VOD rework.
6. **Channel editor UI:** a Triptych toggle + salt field; the tags/filters controls now
   genuinely drive the (vertical-scoped) lineup. EPG block titles show the center's
   title; listing the sides in the program description is a nice-to-have, not required.

### Feeder dispatch after the rework

`_feeder` walks `_stash_schedule` as today; per **channel** (not per block), if the
channel is `triptych`, each schedule entry is played via a composite round — sides
resolved deterministically at feed time (not stored in the schedule), `-t` bounded,
health-checked per rework A. `_feeder_vertical`'s round-spawning mechanics
(`_feed_one_vertical_round`, silent-center safety net, spawn ordering) survive as the
triptych round player; its infinite fresh-random loop does not.

### Tests

Migration one-shot (creates the channel once, honors user deletion); vertical-scoped
lineup build; seeded-sides determinism (same seed inputs → same sides; salt change →
different sides); EPG entries for a triptych channel; mid-block phase math; shorts
standard + triptych coexistence; feeder dispatch per channel type.

## Vertical TV channel — `api/live_tv_engine.py`

> **Superseded by § Approved rework B above** — this section documents the synthetic
> channel that currently ships; rework B replaces it with a per-channel triptych toggle.

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

**Short/partial center audio:** a center whose **audio track is shorter than its video**
(e.g. 30 s of audio under a 118 s clip) would EOF the audio sub early; the parent's keepalive
FD hides that EOF from the master, which then blocks waiting to interleave audio with the
remaining video and **freezes the whole composite at the audio's end** (looks like a hard
stall at a fixed timestamp). `build_audio_cmd` adds **`apad`** (paired with `-t`) on the VOD
path so the audio sub always emits silence-padded audio for the full run length. The
fully-silent case (no audio stream at all) `apad` can't help — that's the filler net below.

**Silent-center safety net:** a center with no audio stream makes `-map 0:a:0?` map nothing,
so the audio sub exits immediately — and the master, still mapping `1:a:0`, then blocks
forever waiting for audio it never receives (the parent holds a keepalive writer FD on the
audio pipe, so the master never sees EOF), which backpressures and **stalls the composite** —
no segments, so a VOD launch fails its readiness gate and a channel wedges every viewer.
Both playout paths carry the same fast-fail pattern `_feed_one_scene` established: if the
audio sub exits within 2 s, it's replaced with an `lavfi` silence filler so the encode still
runs for the center's duration. `_VerticalSessionManager._start_run` applies it per VOD run
(so relaunches/backfill of a silent center are covered too); `_feed_one_vertical_round`
applies it per channel round.

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
keys' coercion, env-override, and save/load round-trip. `tests/test_vertical_engine.py`
covers the synthetic VOD playlist (segment count + uniform/final durations), the segment
range tracker (produced indexes, first gap, run head/coverage, range summary, side phasing),
the `ensure_segment` trigger cases (cache hit, near-head wait, seek-past-head, back-seek gap,
post-reap resume, cold launch) and relaunch debounce, the two-stage teardown (stage-1 reap
keeps segments, stage-2 destroy frees the slot) and fetch liveness reset, backfill scheduling
and preemption, the pacing invariant (Live TV keeps `-re`, VOD drops it / adds `-readrate`),
session-id validation, and the `VERTICAL_DEBUG` level gating.

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
| `VERTICAL_IDLE_TIMEOUT` | `60` | int | Stage-1: seconds of no fetches before the FFmpeg processes are reaped (segments kept) |
| `VERTICAL_SESSION_TTL` | `1800` | int | Stage-2: seconds of no fetches before the cached segments are deleted and the slot freed |
| `VERTICAL_READY_SEGMENTS` | `2` | int | Segment files encoded before PlaybackInfo returns |
| `VERTICAL_READRATE` | `0.0` | float | Input read-rate cap for the VOD subs; `0` = unlimited full-speed encode |
| `VERTICAL_MAX_SESSIONS` | `2` | int | Concurrent VOD composite sessions; over cap → single-video fallback |
| `VERTICAL_HWACCEL` | `"auto"` | enum | VOD master encoder: `none/nvenc/qsv/vaapi/auto` |
| `VERTICAL_DEBUG` | `false` | bool | Verbose diagnostics at INFO (selection pools, FFmpeg cmds, session decisions) |
| `ENABLE_VERTICAL_TV_CHANNEL` | `false` | bool | Enable the always-on Vertical TV Live TV channel |
| `VERTICAL_TV_CHANNEL_NUMBER` | `9000` | int | Channel number for Vertical TV |

`VERTICAL_ASPECT_MIN` and `VERTICAL_READRATE` are the **float** config keys — a float
bucket in `_coerce_config_value()`, and the settings form's numeric submit path
(`templates/components/scripts.html`) picks `parseFloat` over `parseInt` when a key's
`DEFAULTS` entry is non-integer (previously `parseInt("1.3")` would have silently saved `1`).
`VERTICAL_READRATE` is a float whose default is `0` (an integer to JS), so it's listed
explicitly in the form's `FLOAT_KEYS` set to force `parseFloat`.

## Debugging / logs

### Turning on verbose diagnostics

Flip **`VERTICAL_DEBUG`** any of the usual three ways:

1. **Settings UI** — Library tab → "Vertical Multi-View" card → *Verbose Diagnostics*
   toggle → Save. **Takes effect immediately** (the flag is read at log-call time; no
   restart).
2. **Config file** — `VERTICAL_DEBUG = true` in `stash_jellyfin_proxy.conf` (read at
   startup).
3. **Env var** — `VERTICAL_DEBUG=true` on the container (read at startup; wins over the
   file).

With it on, everything the feature knows is written at INFO into the normal proxy log:
per-slot selection decisions with pool sizes and effective weights, the fallback path
taken and final clip ids, the full master/composite/audio FFmpeg command lines, pipe
backend choice + both master attach confirmations, seek decisions (including "no
position given → steady state"), concurrency count vs cap at each launch, and session
create/teardown with the reason.

**Why a config flag instead of `setLevel()`:** hypercorn's `serve()` runs
`logging.config.dictConfig()` during startup, which resets every named logger's level —
any "set the vertical loggers to DEBUG" call made at import time is silently wiped.
`core.vertical.vdebug()` instead chooses the record's level per call (INFO when the
flag is on, DEBUG otherwise), which no dictConfig reset can undo. Same constraint that
motivated the handler-filter approach in `main.py` (`_SuppressLibraryDebugFilter`).

The alternative — global `LOG_LEVEL=DEBUG` — also works (vdebug lines are plain DEBUG
records then) but requires a restart and drowns the log in unrelated debug output.

### Where the logs land

| What | Where |
|---|---|
| Proxy log (lifecycle, selection, decisions) | `{LOG_DIR}/{LOG_FILE}` (also the UI log viewer) |
| VOD compositor FFmpeg stderr, per session | `{LOG_DIR}/vertical_ffmpeg/{session_id}.log` (+`.old` after 10 MB) |
| Vertical TV channel FFmpeg stderr | `{LOG_DIR}/livetv_ffmpeg/{channel_id}.log` (rounds tagged `[round center=…]`, subs `[<center>/composite]` / `[<center>/audio]`) |
| Encoder probe result | proxy log at startup (`H.264 encoder: 'auto' → …`) |

Each per-session file begins with a `===== FFmpeg session start … =====` banner carrying
center/sides/backend/encoder/seek, then the exact master/composite/audio commands, then
interleaved stderr (`[composite]` / `[audio]` prefixes; master lines unprefixed), and ends
with `===== FFmpeg session end (<reason>) =====`.

**`LOG_DIR` must be writable by the container user** (on the Unraid deployment it's
`/config`) — if the `vertical_ffmpeg/` dir can't be created, the engine logs one warning
and continues without the per-session file, so a missing file is itself a signal that
`LOG_DIR` is wrong.

### Reading a failure

- **Play fell back to single video** → proxy log says why: concurrency cap (with the
  session ids holding slots), no side clips (selection warning right above it), or a
  launch failure (paired with the session log's stderr).
- **Session died mid-play** → the session log's last stderr lines; the proxy log has the
  teardown reason line (stage-1 reap / stage-2 destroy, each with its reason).
- **Black/frozen lanes** → composite sub stderr (`[composite]` lines) — look for HTTP
  reconnects against Stash or filtergraph errors.
- **No audio** → `[audio]` lines; a silent center exits the audio sub almost immediately
  (both VOD and Vertical TV: a silence filler is
  spawned and logged).
- **Client stalls after a seek** → the relaunch line for that segment (trigger + old→new
  `start_index` + produced ranges); if the segment stays unavailable the per-request warning
  `segment … unavailable … client retries` fires (the respin didn't produce it in time).