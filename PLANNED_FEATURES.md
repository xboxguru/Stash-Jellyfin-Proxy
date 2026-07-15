# Planned Features

> Master planning doc for Stash-Jellyfin-Proxy. Each feature is self-contained under its own
> `## Feature N` heading. Cross-cutting patterns live once in **§ Shared Conventions** so
> individual features don't repeat them. Add new features by copying **§ Feature Template**.
>
> Status legend: 🟡 Planning · 🔵 Approved · 🟢 In progress · ✅ Done · ⚪ Deferred
>
> **Note:** `*.md` is git-ignored (commit `0a2810e`). This file lives in the tree but isn't
> tracked unless you `git add -f` it. Decide whether to un-ignore plan docs.

## Feature Index

| # | Feature | Status | Summary |
|---|---|---|---|
| 1 | Vertical Multi-View ("Triptych") | ✅ Shipped — see [docs/Triptych.md](docs/Triptych.md) | Toggleable vertical-only library + composite playback + an always-on Vertical TV channel. |
| 2 | [Client-Forced Transcoding via Stash](#feature-2--client-forced-transcoding-via-stash) | ✅ Shipped | Honor the client's "Play with → Transcoding" choice by proxying Stash's on-the-fly HLS transcode (Stash does the work; we proxy). Auto bitrate→resolution downscaling descoped — see §2.11 Phase 2. |
| 3 | Interactive Toy (Handy) Sync | 🟢 Shipped | Drive a Handy from the proxy, synced to the Jellyfin client's playback. **Implemented — see [docs/handy_integration.md](docs/handy_integration.md).** |
| 4 | _(reserved)_ | — | _next idea_ |

---

## Shared Conventions

Patterns every feature below should follow, so each feature section can reference these instead
of restating them.

### Config changes — edit all four places
Config is flat module-level vars in [config.py](config.py). Any new setting must be added to
**all four** spots or it silently won't persist/override:
1. Default value in the defaults block (L17-80).
2. `save_config()` `keys_to_save` list (L98-117).
3. `_coerce_config_value()` type bucket (L135-158) — int / bool / list / set / str.
4. `_supported_keys` env-override list (L184-203).
Settings also need a UI control in
[templates/components/tab_settings.html](templates/components/tab_settings.html) wired to the
existing settings save flow.

### Library registration
Home-screen libraries are built in `_get_libraries()`
([api/library_routes.py:71-103](api/library_routes.py#L71-L103)) via
`build_folder(name, encode_id("root","<key>"), ...)`, gated behind a config flag. Browsing a
root key is handled in `_handle_library_browse`. Scene queries go through `StashQueryBuilder`
([core/query_builder.py](core/query_builder.py)) / `stash_client.fetch_scenes`.

### Playback / transcode wiring
`endpoint_playback_info` ([api/stream_routes.py:14](api/stream_routes.py#L14)) returns
MediaSources. For anything requiring a custom transcode, advertise an HLS **TranscodingUrl
(SubProtocol=hls, direct play disabled)** — the same approach proven for Live TV — and add a
guard/redirect branch in `endpoint_stream` (see the existing Live TV guards at L156-175).

### FFmpeg playout spine (reuse, don't reinvent)
The live-TV engine ([api/live_tv_engine.py](api/live_tv_engine.py)) is the reference for any
long-lived FFmpeg work: a master HLS process normalized to **1920×1080 / yuv420p / 30 fps** +
**s16le 48 kHz stereo**, fed over a pipe backend (`_FifoPipeBackend` Linux /
`_TcpRelayPipeBackend` Windows). Keep the **separate video/audio sub-process split** — the
single-process backpressure deadlock is documented and real
([live_tv_engine.py:459-471](api/live_tv_engine.py#L459-L471)). Read inputs with `-re` so the
client can't outrun the encoder ([L491-496](api/live_tv_engine.py#L491-L496)).

### Logging
Per-session/per-channel FFmpeg log files follow the rotation pattern at
[live_tv_engine.py:51-77](api/live_tv_engine.py#L51-L77). Every feature must log lifecycle
(create/teardown + reason), key decisions (one concise INFO summary + DEBUG detail), full
FFmpeg commands at DEBUG, and errors/fallbacks explicitly.

### Hardware encoding (if a feature transcodes)
Ship CPU + NVENC + QSV/VAAPI selectable via a `*_HWACCEL` setting with a startup probe that
falls back to CPU and logs the effective encoder. **Shared implementation:**
`core/hw_encoder.py` (`resolve_h264_encoder`) — a framework-free, engine-agnostic probe (a real
one-frame test-encode, not an `-encoders` string match), cached per `(mode, ffmpeg_bin)`.

The image is based on **jellyfin-ffmpeg7** ([Dockerfile](Dockerfile),
`FFMPEG_PATH=/usr/lib/jellyfin-ffmpeg/ffmpeg`) — one maintained binary carrying NVENC + QSV +
VAAPI + AMF with the Intel drivers bundled.

**Docker prerequisites to document for users:** NVENC → NVIDIA Container Toolkit + `--gpus all`
+ host driver; Intel QSV/VAAPI → `--device /dev/dri:/dev/dri`; CPU → nothing (default fallback).

Probe/fallback behavior, the image-size tradeoff, and the encoder-only-vs-GPU-decode decision
are documented in [docs/Triptych.md](docs/Triptych.md) → Hardware encoding (Feature 1's
implementation, reusable as-is by any later transcoding feature).

### Testing
Add unit tests under `tests/` mirroring existing style (`test_stream_routes.py`,
`test_config.py`, etc.): selection/business logic, config round-trip for new keys, and
PlaybackInfo behavior.

---

## Feature 1 — Vertical Multi-View ("Triptych")

> Status: ✅ Shipped. Full design, architecture, and implementation details live in
> **[docs/Triptych.md](docs/Triptych.md)**.

---

## Feature 2 — Client-Forced Transcoding via Stash

> Status: ✅ Shipped (2026-07-15) · When a Jellyfin client picks
> "Play with → Transcoding", proxy Stash's on-the-fly HLS transcode endpoint instead of direct
> play. **Stash does the transcoding; we only proxy the stream** — no local FFmpeg/CPU, no
> jellyfin-ffmpeg dependency for this feature. **Phase 2 (§2.11) shipped forced-transcode routing
> across real clients + a Firefox Web fix; automatic bitrate→resolution downscaling was descoped.**

### 2.1 Current state (what already exists)
> **Line refs refreshed 2026-07-15** after Features 1 & 3 shipped (they shifted this file down).

The proxy already proxies Stash's HLS transcode (`/scene/{id}/stream.m3u8` + segments) — but
**only when the proxy auto-detects an incompatible codec/container**, never as a user choice:
- `_requires_transcode()` ([api/stream_routes.py:57](api/stream_routes.py#L57)) and the mirror
  in `_build_media_sources()` ([core/jellyfin_mapper.py:168](core/jellyfin_mapper.py#L168))
  gate everything on codec/container. **The safe-codec/container lists are duplicated in both
  spots — keep them in sync (or extract a shared helper).**
- PlaybackInfo advertises a `TranscodingUrl` **only** when `needs_transcode` is true
  ([jellyfin_mapper.py:197-198](core/jellyfin_mapper.py#L197-L198)); compatible files get
  DirectPlay and **no** transcode URL (the `else` branch at
  [jellyfin_mapper.py:200](core/jellyfin_mapper.py#L200)).
- `endpoint_stream` gates the HLS path on `_requires_transcode`
  ([stream_routes.py:214-222](api/stream_routes.py#L214-L222)); a forced `master.m3u8` on a
  compatible file falls through to raw passthrough (serves mp4 bytes for an `.m3u8` request →
  broken).
- Client quality params (`MaxStreamingBitrate`/`maxWidth`) are never read → transcoding can't
  reduce bandwidth.

### 2.2 Decisions locked in
| Topic | Decision |
|---|---|
| **Transcoder** | **Stash** does the work via its HTTP transcode endpoint; proxy only relays. No local FFmpeg. |
| **Advertise scope** | **Every item** advertises a `TranscodingUrl` (alongside DirectPlay for compatible files), so "Play with → Transcoding" is offered everywhere. Clients still default to DirectPlay unless the user forces transcode. |
| **Format** | **HLS only** — reuse the existing `_rewrite_hls_playlist` + segment proxy. |
| **Quality** | **Honor requested quality if the client sends it**, else transcode at original resolution. Map `MaxStreamingBitrate`/`maxWidth`/`maxHeight` → Stash `resolution`. |
| **Kill-switch** _(decided 2026-07-15)_ | Ship a `ENABLE_FORCED_TRANSCODE` config flag (**default `True`**) in Phase 1 — instant off-switch if dual-advertising makes any client prefer transcode by default (see §2.10). Wired through all four config places + a UI control (§ Shared Conventions → Config changes). When off, behavior reverts to today's codec-gated auto-transcode. |
| **Seek** _(decided 2026-07-15)_ | **Client-native HLS seek.** Serve the full VOD playlist unchanged and let the client seek within it — no `start=` handling on the `.m3u8` branch. Revisit only if seeking breaks in testing. |

### 2.3 Open decisions / to verify
1. **Does Wholphin send quality params?** Investigate the actual transcode URL Wholphin builds
   when "Transcoding" is chosen (capture it in logs). Determines whether quality-mapping fires
   in practice or always falls back to original.
2. **Stash stream `resolution` accepted values.** The GraphQL filter enums in
   [query_builder.py:9-24](core/query_builder.py#L9-L24) are for *finding* scenes, **not** the
   transcode stream endpoint. Confirm what the `/scene/{id}/stream.m3u8?resolution=` HTTP param
   accepts on the target Stash version (likely `ORIGINAL/LOW/STANDARD/STANDARD_HD/FULL_HD/
   FOUR_K` or a pixel height) and build the Jellyfin→Stash mapping table from that.
3. **Audio/bitrate mapping depth** — Phase 1 maps video resolution only; map audio
   channels/bitrate later if needed.

### 2.4 User-facing behavior
- Every scene exposes a "Transcoding" option in the client's "Play with" menu.
- Choosing it streams Stash's HLS transcode through the proxy; choosing MPV/ExoPlayer/Direct
  still direct-plays compatible files unchanged.
- If the client requests a specific quality, the stream is downscaled by Stash; otherwise it's
  transcoded at original resolution (codec normalization).
- Incompatible-codec files keep auto-transcoding exactly as today (no regression).

### 2.5 Architecture
No new engine — refactor the existing stream path (Shared Conventions → Playback/transcode
wiring). Route by **URL intent/extension**, not codec:
- `.m3u8` request ⇒ always serve Stash HLS transcode via `_rewrite_hls_playlist`.
- `/stream` (no transcode marker) ⇒ direct passthrough as today.
- Keep `_requires_transcode` only to decide the *default* (DirectPlay vs forced) advertised in
  PlaybackInfo — not to block the transcode path when the client explicitly asks for it.

### 2.6 Key technical details
- **PlaybackInfo** ([jellyfin_mapper.py:_build_media_sources](core/jellyfin_mapper.py#L168)):
  always include `TranscodingUrl=/Videos/{item_id}/master.m3u8`, `TranscodingSubProtocol=hls`,
  `SupportsTranscoding=true`. For compatible files *also* keep `SupportsDirectPlay/Stream=true`
  + `DirectStreamUrl` so the client chooses (edit the `else` branch at
  [jellyfin_mapper.py:200](core/jellyfin_mapper.py#L200) to add the TranscodingUrl + flip
  `TranscodingSubProtocol` to `hls`). For incompatible files keep direct play disabled. Gate the
  always-advertise behavior on `ENABLE_FORCED_TRANSCODE` (§2.2) — when off, fall back to today's
  needs-transcode-only advertisement.
- **master.m3u8 handler** ([stream_routes.py:endpoint_stream](api/stream_routes.py#L169)):
  serve the rewritten Stash HLS whenever the path ends in `.m3u8`, regardless of
  `_requires_transcode`. Preserve the existing Live TV + Vertical guards already at the top of
  `endpoint_stream` ([stream_routes.py:176-195](api/stream_routes.py#L176-L195)).
- **Seek:** client-native (§2.2) — do **not** thread `StartTimeTicks`/`start=` into the `.m3u8`
  branch; serve the full VOD playlist.
- **Quality mapping (Phase 2):** parse `MaxStreamingBitrate`, `maxWidth`/`maxHeight` (and any
  explicit `videoBitRate`) from the transcode URL query; translate to Stash's `resolution` param
  appended to `/scene/{id}/stream.m3u8`. Absent → omit (Stash original). Mapping table built from
  §2.3.2.
- **Segments** already proxied at
  [stream_routes.py:endpoint_hls_segment](api/stream_routes.py#L249). **Watch-item for Phase 2:**
  `_rewrite_hls_playlist` currently strips the query when rewriting each segment line
  (`line.split('?')[0]`, [stream_routes.py:81](api/stream_routes.py#L81)), so a `resolution`
  param cannot simply ride along. The resolution must be re-encoded into the rewritten segment
  URL and re-appended by `endpoint_hls_segment` when it re-requests from Stash — verify whether
  Stash varies segment basenames by resolution first, since that decides whether the param must
  be carried explicitly.
- **Subtitles:** unchanged — keep the existing external-subtitle delivery
  ([stream_routes.py:endpoint_subtitle](api/stream_routes.py#L116)); no burn-in.

### 2.7 Code touch-points
**Modified:**
- `core/jellyfin_mapper.py` `_build_media_sources` (L168-201) — always advertise TranscodingUrl;
  dual-advertise for compatible files (gated on `ENABLE_FORCED_TRANSCODE`).
- `api/stream_routes.py` `endpoint_stream` (L169) — route `.m3u8` to HLS transcode
  unconditionally; `_rewrite_hls_playlist` (L70) and `endpoint_hls_segment` (L249) — thread
  through `resolution` (**Phase 2 only**; no `start` threading — seek is client-native).
- Possibly a small helper `_stash_transcode_url(raw_id, quality_params)` to centralize the
  Stash URL + resolution mapping (Phase 2).
**Config changes (Phase 1) — `ENABLE_FORCED_TRANSCODE` (bool, default `True`):** add to all four
config places (§ Shared Conventions → Config changes) **and** a UI toggle in
`templates/components/tab_settings.html`. *(Optional later: a `TRANSCODE_DEFAULT_RESOLUTION`
cap.)*
**Tests:** extend `test_stream_routes.py` (PlaybackInfo now always advertises TranscodingUrl;
`.m3u8` on a compatible file returns rewritten HLS, not passthrough; quality param maps to a
Stash `resolution`) and `test_jellyfin_mapper.py` (MediaSources dual-advertise).

### 2.8 Logging
Log: the chosen play path (DirectPlay vs forced transcode) per PlaybackInfo; the full client
transcode URL **including any quality params** (critical for the §2.3.1 Wholphin investigation);
the resolved Stash transcode URL (+ resolution) we proxy to; HLS playlist rewrite success/fail
(already partially logged at [stream_routes.py:58,73](api/stream_routes.py#L58)); fallbacks when
no quality param is present.

### 2.9 Phasing
- **Phase 1 — Forced transcode plumbing.** Always-advertise TranscodingUrl (gated on
  `ENABLE_FORCED_TRANSCODE`) + route `.m3u8` to Stash HLS regardless of codec + dual-advertise
  compatible files + config flag (four places + UI). Client-native seek. Makes "Transcoding" work
  at original resolution. Shippable.
- **Phase 2 — Quality mapping.** Capture Wholphin's params (§2.3.1), confirm Stash `resolution`
  values (§2.3.2), implement the Jellyfin→Stash quality map + the segment-variant threading
  watch-item (§2.6). Makes "Transcoding" actually save bandwidth.

### 2.11 As implemented
> _(Fill in per chat as work lands — record where the implementation diverged from this plan:
> final function names/signatures, actual config keys, the confirmed Stash `resolution` values &
> mapping table, the captured Wholphin transcode URL, and any client-behavior findings. Each
> chat updates this section before finishing.)_

**Phase 1 — Forced transcode plumbing (shipped 2026-07-15).**

_Config key:_ `ENABLE_FORCED_TRANSCODE` — `bool`, default `True`. Wired through all four
[config.py](config.py) places (defaults block, `save_config` `keys_to_save`, `_coerce_config_value`
bool bucket, `_supported_keys`) + a UI toggle "Offer Forced Transcoding" in the **Client Behavior**
card of [templates/components/tab_settings.html](templates/components/tab_settings.html) (plus the
`DEFAULTS` map in [scripts.html](templates/components/scripts.html)). Load/save is generic-by-name
via the existing `api_get_config`/`api_post_config` flow — no per-key handler needed.

_Final signatures (all unchanged from before — gating is internal):_
- `_build_media_sources(item_id, path, files, runtime_ticks, title, captions=None, vertical=False)`
  ([core/jellyfin_mapper.py:168](core/jellyfin_mapper.py#L168)).
- `endpoint_stream(request)` ([api/stream_routes.py:169](api/stream_routes.py#L169)).
- `_rewrite_hls_playlist(stash_base, raw_id, item_id, apikey)` ([api/stream_routes.py:70](api/stream_routes.py#L70)) — unchanged; no `start=`/resolution threading (client-native seek per §2.2).

_Diverged from plan:_
- **Mapper — new branch, not an edited `else`.** The plan said to add the TranscodingUrl into the
  existing `else` branch. Implemented instead as a **distinct `elif forced_transcode:` branch**
  (compatible + flag on → dual-advertise) with the `else` reserved for **flag-off = directplay-only**
  (`TranscodingSubProtocol: "http"`, no `TranscodingUrl`). Branch order is
  `vertical → needs_transcode → forced_transcode → else`, so incompatible files and Vertical are
  untouched. Each branch sets a `play_path` label logged at INFO (§2.8).
- **`endpoint_stream` gate.** `.m3u8` is served as HLS when
  `path_is_m3u8 and (needs_transcode or forced_enabled)`. Consequence: an incompatible file always
  serves HLS (flag-independent, no regression); a **compatible** `.m3u8` serves HLS only when the
  flag is on — flag-off falls through to raw passthrough, which is exactly today's (pre-feature)
  behavior. The non-`.m3u8` incompatible redirect-to-`master.m3u8` is preserved.
- **Test harness.** `tests/conftest.py` only registered `/videos/{item_id}/stream`; added
  `/videos/{item_id}/master.m3u8` and `/videos/{item_id}/hls/{segment}` routes so the `.m3u8` path
  is exercisable end-to-end in tests.

_Logging (§2.8):_ chosen play path per PlaybackInfo — `core.jellyfin_mapper` INFO
`PlaybackInfo MediaSource: item=… play_path=… codec=…/… forced_transcode=…`; forced-transcode
routing — `api.stream_routes` INFO `Stream: HLS transcode scene=… client_url=… needs_transcode=…
forced_enabled=…`; resolved Stash URL — `HLS: proxying Stash transcode scene=… -> …/stream.m3u8`
(apikey redacted).

_Client-behavior findings:_ Verified end-to-end by driving the **real** `main.app` (full route
table) through Starlette's TestClient with only outbound Stash calls mocked — all seven flag×codec
paths behaved as specified, incl. HEAD probes on the forced `.m3u8` (200, `application/x-mpegURL`,
empty body). Dual-advertise keeps `SupportsDirectPlay=True`, so the client's **default is still
DirectPlay** — the transcode is an offered option, not forced. **Not yet confirmed on a real
Wholphin/other client** (no device in this session): the §2.10 "does dual-advertising make any
client prefer transcode by default?" check and the §2.3.1 Wholphin quality-param capture remain for
Phase 2 / real-device testing.

**Phase 2 — Real-device transcode routing + Firefox fix (shipped 2026-07-15).**

> Phase 2 diverged hard from the "quality mapping" plan after a real-device investigation
> (§2.3.1/§2.3.2). The user's revised goal: **make forced transcode work when a client explicitly
> selects it, and fix Jellyfin Web/Firefox** — **not** automatic bitrate→resolution downscaling.
> The Stash resolution mapping was fully confirmed but deliberately **not wired up** (see below).

_§2.3.2 — Stash `resolution=` values (CONFIRMED empirically, not assumed)._ On the target Stash,
`/scene/{id}/stream.m3u8?resolution=` honors **only** the `StreamingResolutionEnum` names; every
other value (pixel heights, `*p` labels, the `query_builder.py` GraphQL enums like `VERY_LOW`/
`WEB_HD`, garbage) is **silently ignored → original**. Verified by ffprobing segments of a
1920×1080 source (scene 36892):

| `resolution=` | decoded | | `resolution=` | decoded |
|---|---|---|---|---|
| _(none)_ / `ORIGINAL` | 1920×1080 | | `FULL_HD` | 1920×1080 (=source) |
| `LOW` | 426×240 | | `FOUR_K` | 1920×1080 (capped, no upscale) |
| `STANDARD` | 854×480 | | `VERY_LOW`,`240`,`480`,`240p`,`bogus` | 1920×1080 (**ignored**) |
| `STANDARD_HD` | 1280×720 | | | |

**Mapping table** (built but unused — kept for a future client that sends real quality): `LOW`=240,
`STANDARD`=480, `STANDARD_HD`=720, `FULL_HD`=1080, `FOUR_K`=2160, `ORIGINAL`=source.

_Segment-basename finding (§2.6)._ The playlist endpoint accepts **any** `resolution` (even
`bogus`→200) and always returns **identical** segment basenames (`0.ts`, `1.ts`…) — the resolution
only takes effect when applied to the **segment** request. So if quality threading is ever added,
`resolution` MUST be carried explicitly into the rewritten segment URLs and re-appended in
`endpoint_hls_segment` (basename alone can't select a variant). Not implemented in Phase 2.

_§2.3.1 — how real clients actually request transcode (captured 2026-07-15)._ The advertised
`TranscodingUrl` is fetched **bare** (`query_params={}`) — no client puts quality on it. Two
distinct mechanisms:

| Client | Forces transcode | Quality signal | Dual-advertise |
|---|---|---|---|
| Wholphin, Fladder (`client='Dart'`) | `EnableDirectPlay=false` in PlaybackInfo POST body ✅ | none (100 M / ∞ sentinel) | tolerates |
| Jellyfin Android | ❌ never | `DeviceProfile.MaxStreamingBitrate` (body); dropped 100M→5M→1M on quality pick | tolerates |
| Jellyfin Web (Chrome) | ❌ never | `MaxStreamingBitrate` (PlaybackInfo **query**) | tolerates |
| Jellyfin Web (Firefox) | ❌ never | `MaxStreamingBitrate` (query); bitrate-test returns garbage (1600–11200 bps) | **deadlocks** |
| Findroid | ❌ never | `MaxStreamingBitrate` (body top-level + DeviceProfile); no quality selector | tolerates |

Only the flag-forcers (Wholphin/Fladder) have a usable "force transcode" affordance; the official
apps + Findroid rely on the *server* to switch them to transcode by comparing requested bitrate to
source (which we intentionally do **not** do — the user declined bitrate-based downscaling).

_What was implemented:_
1. **Honor the client's requested delivery methods.** PlaybackInfo POST with `EnableDirectPlay=false`
   + `EnableTranscoding=true` (Wholphin/Fladder "Play with → Transcoding") now sets
   `force_transcode_only` → the MediaSource advertises **HLS-only** (`SupportsDirectPlay=false` +
   `TranscodingUrl`), so the client actually fetches `master.m3u8`. Verified end-to-end on both
   clients (`play_path=forced-hls (client-requested)` → segments streamed). Signatures:
   `endpoint_playback_info` parses the flag; `format_jellyfin_item(..., force_transcode_only=False)`
   and `_build_media_sources(..., force_transcode_only=False)` thread it through.
   **Confirmed safe:** normal play always sends `EnableDirectPlay=true` → unaffected.
2. **Removed the dual-advertise (§2.2 reversal).** A compatible file on a *normal* play now
   advertises **DirectPlay only, no `TranscodingUrl`** — even with the kill-switch on. Advertising a
   `TranscodingUrl` *alongside* DirectPlay made **Firefox Jellyfin Web** load hls.js and then
   deadlock against the direct `.mp4` (16–20 s hangs; the §2.10 risk, realized). Forced transcode is
   still fully reachable on demand (clients re-request with the flag; `SupportsTranscoding=true`
   stays advertised so the menu still appears — verified). `ENABLE_FORCED_TRANSCODE` now only gates
   whether an explicit client transcode request is honored (off → ignored → DirectPlay).

_Diverged from plan:_
- **No bitrate→resolution quality mapping / segment threading** — descoped by the user (2026-07-15):
  "I don't really care about trying to detect from bitrate." The confirmed mapping table + the
  segment-basename requirement are recorded above for a future revisit.
- **`§2.2 "every item dual-advertises TranscodingUrl"` reversed** — it was the direct cause of the
  Firefox deadlock; TranscodingUrl is now advertised only when actually transcoding.
- No new config key (the planned `TRANSCODE_DEFAULT_RESOLUTION` was part of the descoped downscaling).

_Logging (§2.8):_ `F2 PlaybackInfo request` (client + method + query) and `F2 PlaybackInfo flags`
(Enable* + MaxStreamingBitrate) at INFO; raw body + full DeviceProfile at DEBUG; the mapper's
`play_path=…` line records the routing decision; `F2 transcode request … query_params=…` +
`HLS: proxying Stash transcode …` on the forced path.

_Client-behavior findings:_ **Firefox Web fixed** (direct-plays, no hang — user-confirmed +
zero >8 s stream completions post-fix). **Wholphin & Fladder** forced transcode works via the flag.
**Chrome / Android / Findroid** direct-play cleanly and tolerate the change. The Firefox
bitrate-test returning garbage (1600–11200 bps) is a separate, pre-existing oddity, now harmless
since we don't act on the bitrate.

### 2.10 Risks / watch-items
- **Verify Stash `resolution` HTTP values** before coding the map (don't assume the GraphQL
  enums apply).
- **Don't regress auto-transcode** for genuinely incompatible files or **Live TV channel
  guards** in `endpoint_stream`.
- **Client behavior:** confirm advertising both DirectPlay + TranscodingUrl doesn't make any
  client *prefer* transcoding by default (it shouldn't, but verify in Wholphin + one other).
- **Seek under transcode:** ensure `StartTimeTicks`/HLS seeking still works through the proxied
  Stash playlist.

---

## Feature 3 — Interactive Toy (Handy) Sync

> Status: 🟢 **Shipped** (bench-confirmed on Handy FW 4.2.2). The full design, architecture, wire
> protocol, and implementation details now live in a dedicated dev-facing document:
> **[docs/handy_integration.md](docs/handy_integration.md)**.

---

## Feature Template

> Copy this block for each new feature. Reference **§ Shared Conventions** instead of
> restating common patterns. Keep "Open decisions" explicit so they're easy to resolve before
> coding.

```
## Feature N — <name>

> Status: 🟡 Planning · <one-line summary>

### N.1 Decisions locked in
| Topic | Decision |
|---|---|
|  |  |

### N.2 Open decisions (proposed defaults — confirm)
1.

### N.3 User-facing behavior

### N.4 Architecture
<what existing spine/modules it reuses; new modules>

### N.5 Key technical details
<FFmpeg/algorithm/data specifics>

### N.6 Code touch-points
<files: new + modified; config keys (all four places + UI)>

### N.7 Logging
<lifecycle / decisions / errors>

### N.8 Phasing
<incremental, shippable slices>

### N.9 Risks / watch-items
```
