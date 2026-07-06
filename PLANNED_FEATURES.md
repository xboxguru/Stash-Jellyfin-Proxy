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
| 2 | [Client-Forced Transcoding via Stash](#feature-2--client-forced-transcoding-via-stash) | 🔵 Approved | Honor the client's "Play with → Transcoding" choice by proxying Stash's on-the-fly HLS transcode (Stash does the work; we proxy). |
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

> Status: 🔵 Approved (decisions locked, ready to plan code) · When a Jellyfin client picks
> "Play with → Transcoding", proxy Stash's on-the-fly HLS transcode endpoint instead of direct
> play. **Stash does the transcoding; we only proxy the stream** — no local FFmpeg/CPU, no
> jellyfin-ffmpeg dependency for this feature.

### 2.1 Current state (what already exists)
The proxy already proxies Stash's HLS transcode (`/scene/{id}/stream.m3u8` + segments) — but
**only when the proxy auto-detects an incompatible codec/container**, never as a user choice:
- `_requires_transcode()` ([api/stream_routes.py:44](api/stream_routes.py#L44)) and the mirror
  in `_build_media_sources()` ([core/jellyfin_mapper.py:147](core/jellyfin_mapper.py#L147))
  gate everything on codec/container.
- PlaybackInfo advertises a `TranscodingUrl` **only** when `needs_transcode` is true
  ([jellyfin_mapper.py:162-165](core/jellyfin_mapper.py#L162-L165)); compatible files get
  DirectPlay and **no** transcode URL.
- `endpoint_stream` gates the HLS path on `_requires_transcode`
  ([stream_routes.py:194-202](api/stream_routes.py#L194-L202)); a forced `master.m3u8` on a
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
- **PlaybackInfo** ([jellyfin_mapper.py:_build_media_sources](core/jellyfin_mapper.py#L140)):
  always include `TranscodingUrl=/Videos/{item_id}/master.m3u8`, `TranscodingSubProtocol=hls`,
  `SupportsTranscoding=true`. For compatible files *also* keep `SupportsDirectPlay/Stream=true`
  + `DirectStreamUrl` so the client chooses. For incompatible files keep direct play disabled.
- **master.m3u8 handler** ([stream_routes.py:endpoint_stream](api/stream_routes.py#L156)):
  serve the rewritten Stash HLS whenever the path ends in `.m3u8`, regardless of
  `_requires_transcode`. Preserve existing Live TV channel guards.
- **Quality mapping:** parse `MaxStreamingBitrate`, `maxWidth`/`maxHeight` (and any explicit
  `videoBitRate`) from the transcode URL query; translate to Stash's `resolution` param appended
  to `/scene/{id}/stream.m3u8`. Absent → omit (Stash original). Mapping table built from §2.3.2.
- **Segments** already proxied at
  [stream_routes.py:endpoint_hls_segment](api/stream_routes.py#L222) — ensure any
  `resolution`/start params propagate to the segment URLs in the rewritten playlist so Stash
  serves the matching variant.
- **Subtitles:** unchanged — keep the existing external-subtitle delivery
  ([stream_routes.py:endpoint_subtitle](api/stream_routes.py#L116)); no burn-in.

### 2.7 Code touch-points
**Modified:**
- `core/jellyfin_mapper.py` `_build_media_sources` (L140-166) — always advertise TranscodingUrl;
  dual-advertise for compatible files.
- `api/stream_routes.py` `endpoint_stream` (L156) — route `.m3u8` to HLS transcode
  unconditionally; add quality→resolution param mapping; `_rewrite_hls_playlist` (L57) and
  `endpoint_hls_segment` (L222) — thread through `resolution`/`start`.
- Possibly a small helper `_stash_transcode_url(raw_id, quality_params)` to centralize the
  Stash URL + resolution mapping.
**No config changes required** for the core behavior. *(Optional later: a
`TRANSCODE_DEFAULT_RESOLUTION` cap, or `ENABLE_FORCED_TRANSCODE` kill-switch — only if we want
it toggleable.)*
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
- **Phase 1 — Forced transcode plumbing.** Always-advertise TranscodingUrl + route `.m3u8` to
  Stash HLS regardless of codec + dual-advertise compatible files. Makes "Transcoding" work at
  original resolution. Shippable.
- **Phase 2 — Quality mapping.** Capture Wholphin's params (§2.3.1), confirm Stash `resolution`
  values (§2.3.2), implement the Jellyfin→Stash quality map. Makes "Transcoding" actually save
  bandwidth.

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
