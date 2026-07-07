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
   into `/Videos/{id}/stream` (client bypassing PlaybackInfo) 302-redirects to a composite
   session.

   **Dedupe by scene:** both entry points call `_vertical_manager.session_for_scene(scene_id)`
   and reuse an existing non-stopped session for that scene (preferring a live one, else a
   cached/reaped one) instead of minting a fresh nonce. Some clients (e.g. Wholphin/ExoPlayer)
   query PlaybackInfo *and* build the `/Videos/stream` URL; without dedupe each would start
   its own encode, doubling the CPU and cap/disk pressure. With it, one composite drives the
   play. The nonce still makes a *fresh* play (after the session is torn down) re-roll sides.
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

**TCP-relay throughput (`_RELAY_BUFSIZE`, Windows only):** the FIFO backend forwards bytes
in-kernel, but the Windows TCP relay copies them through a single asyncio loop. At the
default 64 KB `start_server` limit / read size, a ~3 MB raw frame is shuttled in ~48 tiny
chunks and the per-chunk overhead throttles the composite (observed ~0.14× realtime → the
readiness gate timed out). The relay reads and its `StreamReader` `limit` are raised to
**4 MB** (a whole frame per iteration). This is a dev-backend property; the FIFO backend has
no relay in the path and runs full-speed.

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

> **Status: implemented 2026-07-06; field-tested 2026-07-07 on the Windows/TCP-relay dev
> backend** (Wholphin/ExoPlayer), which surfaced and fixed a series of real bugs — see the
> `### Field-test fixes` subsection below. The FIFO/Linux (Unraid) deployment, which has no
> TCP relay in the path, is still the definitive performance/stability target.

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
in lockstep. The master runs `-hls_flags independent_segments+temp_file`: **`temp_file`** is
essential — because the playlist advertises every segment up front, a client can request
seg *N* the instant it seeks there, and without `temp_file` the muxer's `seg{N}.ts` exists
(and would be served) while still being written or left partial by a teardown; `temp_file`
writes `seg{N}.ts.tmp` and renames on finalize, so `seg{N}.ts` only appears whole. The
range tracker's `seg(\d+)\.ts$` anchor excludes the `.tmp` files.

**2. Full-speed encode (backend-aware).** The vertical subs drop `-re` (the Live TV pacing
flag) — the client reads static files off disk, the encoder outruns it, and there is no
client-paced backpressure in the chain. On the **FIFO backend** the subs run at
`VERTICAL_READRATE` (default `0` = unlimited full speed). On the **TCP relay** they are
capped at `_TCP_RELAY_READRATE` (3×) — full-speed raw video overwhelms the single-threaded
relay — but still outrun a 1× client. The video/audio process split stays (the actual
deadlock protection; see § Pipe topology). `-shortest` is a **no-op** on the single-output
composite, so each sub is bounded by an explicit `-t (center_duration − seek)` to exit at
center EOF (see Field-test fix #2); when the subs exit, the master finalizes gracefully on
EOF (fix #3).

**3. Segment range tracker + `ensure_segment(index)` — the single recovery path.** The
session tracks which segment indexes exist on disk (produced ranges; seeks leave holes).
A request for a missing segment calls `ensure_segment(index)`, which decides:

- **within `_FORWARD_WAIT_SEGMENTS` (24) of the live head** → *wait* (`_await_segment`). This
  window must exceed any client read-ahead buffer (ExoPlayer buffers ~30–60 s ≈ 8–15
  segments ahead) — a smaller window mistook read-ahead for a seek and caused a relaunch
  storm (fix #4). The wait is **progress-aware**: it keeps waiting while the encode head
  advances, so a slow-but-working encode is never abandoned.
- **farther ahead, behind the run start, or the encoder is dead** → *relaunch* the subs with
  `-ss index*4` on the center and `-start_number index` on the master, into the *same* dir
  (old segments stay valid).
- **the wait gave up** (head stalled/froze short of the target — e.g. a seek *past* a stuck
  buffer edge) → *relaunch at the index* and wait once more, so a stalled encode recovers
  instead of 404ing forever (fix #6).

That one path serves seek-past-head, seek-into-a-gap, resume-after-reap, and
recover-from-stall identically. A respin never counts against `VERTICAL_MAX_SESSIONS` — same
session. Relaunches serialize on the session lock (a newer target re-decides after the
previous respin lands). A request with **no** position (`None` vs `0.0` through
`_seek_seconds` → `ensure`) is steady-state and never disturbs the running encode. The
segment endpoint 404s a request **past the last real segment** (`index ≥ total`) without
respinning (a player probing beyond EOF).

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

### Field-test fixes (2026-07-07)

Field-testing on the Windows/TCP-relay dev backend surfaced a run of concrete bugs — each
was diagnosed from the per-session FFmpeg log (`vertical_ffmpeg/{session}.log`), **not** from
guessing at "relay flakiness." All are fixed and unit-tested; most are backend-agnostic and
also apply to the FIFO/Linux path.

1. **Ultra-tall clips crashed the composite.** `scale=-2:1080,crop=608:1080` produces a lane
   narrower than 608 for any source taller than 1080/608 ≈ 1.776:1 (e.g. a 720×1282 center →
   606 px), and `crop=608` then aborts the whole composite before its first frame. **Fix:**
   cover-crop — `scale=608:1080:force_original_aspect_ratio=increase:force_divisible_by=2,crop=608:1080`.
2. **`-shortest` is a no-op → the composite hung at center EOF.** With one output stream,
   `-shortest` never fires, so at center EOF `hstack` stalls on the ended input; a short clip
   froze a couple frames short of finalizing its last segment. **Fix:** bound each sub with
   `-t (center_duration − seek)` (the `duration` arg to the shared builders; the Vertical TV
   channel omits it and is byte-identical to before).
3. **Windows hard-kill dropped the last segment.** `proc.terminate()` is `TerminateProcess()`
   on Windows (no graceful flush), so terminating the master at run end left `seg{N}.ts.tmp`
   un-renamed; a 2-segment clip lost `seg1`. **Fix:** `_kill_procs` closes the backend
   (hands the master EOF) and **waits `_MASTER_GRACE_SECS` for it to finalize and exit on its
   own**, hard-killing only if that stalls.
4. **Read-ahead relaunch storm.** `_run_covers` used a 4-segment forward window, so a client
   buffering ~50 s ahead had every read-ahead request treated as a forward seek → relaunch,
   killing the healthy encode. **Fix:** widen the forward-wait window to `_FORWARD_WAIT_SEGMENTS`
   (24), above any client buffer.
5. **Silent / short-audio centers stalled the master.** A center with no audio, *or an audio
   track shorter than its video* (e.g. 30 s audio under a 118 s clip), EOFs the audio sub;
   the parent keepalive FD hides that EOF, so the master blocks waiting to interleave audio
   with the remaining video and the composite freezes exactly at the audio's end. **Fix:**
   `apad` pads the audio to the run length (`-t` truncates it), and `_start_run` still spawns
   an `lavfi` silence filler for the no-audio-at-all case (where `apad` has no input).
6. **A seek past a stall wedged forever.** When an encode froze mid-run (alive but not
   advancing), a seek's segment request kept *waiting* (it was within the forward window) and
   never relaunched. **Fix:** `_await_segment` is progress-aware (waits only while the head
   advances) and `ensure_segment` **relaunches at the index when the wait gives up**, so a
   seek past a stuck edge respins the encode there.

Cross-cutting: the concurrency cap counts **live encodes** (not cached dirs, so a
reaped/finished session doesn't strand a slot); sessions **dedupe by scene** (§ Playback
wiring); the launch **readiness gate runs outside the manager lock** and is progress-aware
(§ Encoders/teardown); and the TCP relay buffer is **4 MB** (§ Pipe topology).

### Encoders, concurrency, two-stage teardown

- **Encoder:** selected by `VERTICAL_HWACCEL` (`none|nvenc|qsv|vaapi|auto`) through the shared
  probe (see § Hardware encoding below). The master's `-c:v` block (plus any device-init /
  `hwupload` filter) is supplied by the probed encoder. Decode + `hstack` still run on CPU.
- **Concurrency:** `VERTICAL_MAX_SESSIONS` (default 2) caps concurrent **live encodes**
  (`active_count` counts sessions with a running master, *not* cached dirs — a reaped or
  finished session burns no CPU and must not strand a slot; a resume respins the same session
  and doesn't re-check the cap). A launch over the cap is refused (logged) → single-video
  fallback. 3 decodes + 1 encode is heavy — the cap is a safety rail (Stash is single-user).
- **Disk guardrail:** a session renders the *full* center clip (~1–2 GB per 30 min at
  1080p30). Before launch the engine checks free space on the HLS temp volume and refuses
  below a 2 GB floor (falling back to single-video playback, logged).
- **Two-stage teardown:** a 20 s watchdog runs two clocks off the last fetch.
  **Stage 1 — process reap** at `VERTICAL_IDLE_TIMEOUT` (default 60 s): kill the subs +
  master, close the pipe backend, but **keep the cached segments** (a no-op if the encode
  already finished). **Stage 2 — session destroy** at `VERTICAL_SESSION_TTL` (default
  1800 s), or an explicit `/stop`: delete the temp dir and free the cap slot. A segment fetch
  is the liveness signal and resets both clocks; a client that unpauses after a reap
  transparently respins via `ensure_segment`.
- **Readiness gate:** PlaybackInfo waits for `VERTICAL_READY_SEGMENTS` (default 2, capped at
  the clip's segment count so an ultra-short clip doesn't hang) finalized segments before
  returning. It runs **outside the manager lock** (`ensure` → `_await_launch_ready`), because
  a short clip's final segment is finalized by the run monitor terminating the master — which
  needs that same lock — so waiting under it would deadlock; it also stops a slow launch from
  blocking other sessions. The gate is **progress-aware**: it fails only if the master dies or
  no new segment appears for `_READY_STALL_SECS`, so a slow-but-working encode isn't dropped
  to single-video. Expected first-frame latency with full-speed encode is ~3–5 s on the FIFO
  backend (vs 15–20 s under the old realtime pacing + 3-segment gate).

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
with reason, concurrency refusals (with the ids of the live encodes holding the slots), the
disk-guardrail refusal (with free space vs floor), the silence-filler spawn on a no-audio
center, and the single-video fallback with its reason. A session that fails the readiness
gate is torn down immediately rather than left encoding orphaned.

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

## Engine stability fixes (rework A) — ✅ IMPLEMENTED 2026-07-07

Four defects from the 2026-07-06 field test are now fixed. All channel types benefit; fixes
integrated into `api/live_tv_engine.py`, `api/vertical_engine.py`, and `core/hw_encoder.py`.

1. **Pipeline throughput:** Live TV master now uses `core/hw_encoder.py` to select hardware
   H.264 encoding (NVENC/QSV/VAAPI, fallback to libx264). Config: `LIVE_TV_HWACCEL` uses
   existing probe/fallback semantics. Chosen vs effective encoder logged per launch.
2. **Round termination:** Both `_feed_one_vertical_round` and VOD compositor now bound
   composite + audio subs with `-t (center_duration − seek)`. Clean exit at center EOF
   instead of hang (where `-shortest` is a no-op on single-output composite).
3. **Master health:** Feeder health-checks the master process before each round and on sub
   failure; master dead → channel teardown + relaunch (loudly logged). Silent-center
   detector now distinguishes "audio stream missing" (clean exit) from "pipe endpoint
   aborted" (non-zero exit).
4. **Tall verticals:** Both VOD and channel use cover-crop lane geometry
   (`scale=608:1080:force_original_aspect_ratio=increase:force_divisible_by=2,crop=608:1080`)
   to handle ultra-tall sources.

**Invariants preserved:** `pace_args=("-re",)` (realtime pacing); video/audio sub split;
VOD full-length-seek behavior all untouched.

## Triptych channels — per-channel toggle (Feature 1 Phase 2 Rework)

Triptych channels are implemented as a per-channel flag in `channels.json`. Any tag/filter/shorts
channel can set `triptych: true` to enable composite playback (center + 2 sides) instead of
single-video playout. The synthetic "Vertical TV" channel has been replaced with this flexible
per-channel approach, eliminating special cases and enabling full EPG/guide support.

### Implementation overview — `api/live_tv_data.py`, `api/live_tv_engine.py`, `core/vertical_selection.py`

**Channel configuration:**
- `triptych: true` (boolean, default `false`) — play all blocks as composite rounds.
- `triptych_salt: ""` (string, default `""`) — free-text seed modifier; changing it re-rolls
  all sides deterministically without rebuilding the lineup.

**Scene filtering:**
Triptych channels apply the vertical predicate (height > width, aspect ≥ 1.3) to the fetched
scene lineup in `_fetch_scenes_for_stash_channel()`. Non-vertical scenes are excluded before
schedule building.

**Schedule building:**
Triptych channels use the same schedule builder as tag/filter/shorts channels. The schedule
consists of center scene entries, scheduled back-to-back, with duration = center file duration
(enforced by rework A's `-t` bound). No special EPG handling — triptych blocks appear as normal
program entries in the guide, titled by the center scene's name.

**Feeder dispatch:**
`_feeder()` checks per-channel if `ch.get("triptych")` is true:
- False → normal single-scene feed path (`_feed_one_scene`)
- True → composite-round feed path (`_feeder_triptych`)

`_feeder_triptych()` walks `_stash_schedule[tvg_id]` the same way the normal feeder walks it:
- Load the next scheduled segment (center scene).
- Compute seeded RNG: `seed_hash = hash((tvg_id, salt, schedule_generation, block_index, center_id))`
- Fetch vertical candidates and use seeded selection (`pick_center_and_sides_seeded()`) to resolve sides.
- Launch a composite round via `_feed_one_vertical_round()` (center duration, 2 looping sides).
- Advance to the next block when the composite finishes.

**Seeded side selection:**
`core/vertical_selection.py` exports seeded versions of the side-picking algorithm:
- `select_side_clips_seeded(center_scene, candidates, rng)` — picks 2 sides using a seeded RNG.
- `pick_center_and_sides_seeded(candidates, center_id, rng)` — resolves a known center + sides.

The seed is stable across channel relaunches and mid-block viewer joins, ensuring the same
triptych composition plays consistently.

**Mid-block seek:**
Joining a block at wall-clock offset *t* computes:
- Center seek: `t - block.start_ts` (standard playlist seek math)
- Side seeks: `(t - block.start_ts) mod side_duration` (phase-aligned in the looping side duration)

The feeder passes per-side seeks to `_feed_one_vertical_round()`, which uses them in
`build_composite_cmd(..., left_seek, right_seek)`.

**Migration from synthetic vertical_tv:**
On startup, if all three flags are true (`ENABLE_STASH_CHANNELS && ENABLE_VERTICAL_TV_CHANNEL &&
ENABLE_VERTICAL_MULTI`) and no "Vertical TV" channel exists in `channels.json`, create one:
- name: "Vertical TV"
- tvg_id: "vertical_tv"
- stash_type: "filter"
- source_ids: [] (empty; all-verticals filter applied by triptych logic)
- triptych: true
- triptych_salt: ""
- channel number: `VERTICAL_TV_CHANNEL_NUMBER`

This is a one-shot migration; the user can freely edit or delete the migrated channel.
The old config keys (`ENABLE_VERTICAL_TV_CHANNEL`, `VERTICAL_TV_CHANNEL_NUMBER`) remain
for migration support but are deprecated post-implementation.

### Tests — `tests/test_live_tv_vertical.py`, `tests/test_vertical_selection.py`

- Migration one-shot: channel created once, honors user deletion.
- Vertical-scoped lineup: triptych channels filter to vertical scenes only.
- Seeded determinism: same seed inputs → same sides; salt change → different sides.
- EPG entries: triptych blocks appear as normal schedule entries in the guide.
- Mid-block phase math: center seek *t*, sides seek *t mod side_duration*.
- Shorts coexistence: standard and triptych shorts channels instantiate independently.
- Feeder dispatch: triptych channels dispatch to composite round feeding.
- Silent-center safety net: audio-sub failure triggers silence filler (reuses VOD path).

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
the `ensure_segment` trigger cases (cache hit, forward read-ahead waits, seek-past-head,
back-seek gap, post-reap resume, cold launch, **recovery relaunch when a wait stalls**) and
relaunch debounce, scene dedupe (`session_for_scene` — prefer live, skip stopped), the
two-stage teardown (stage-1 reap keeps segments, stage-2 destroy frees the slot) and fetch
liveness reset, backfill scheduling and preemption, the command builders (Live TV keeps
`-re` while VOD drops it / adds `-readrate`, cover-crop lanes, the `-t` duration bound, and
`apad` audio padding), session-id validation, and the `VERTICAL_DEBUG` level gating.

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