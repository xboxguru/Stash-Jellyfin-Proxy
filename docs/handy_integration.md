# Handy (Interactive Toy) Sync — Implementation

Drives a connected [Handy](https://www.handyfeeling.com) device in sync with an interactive scene's
funscript while it plays through the proxy on a Jellyfin client (Wholphin / ExoPlayer / MPV / web).
The proxy acts as the Handy controller, driven by the Jellyfin **playback-reporting events** it
already receives — there is no client-side toy support required.

**Status:** implemented and bench-confirmed on **Handy FW 4.2.2+44874492**. Both serving paths work:
HSSP (cloud-hosted script) and HSP (live point-streaming). Disabled by default (`ENABLE_HANDY_SYNC`).

All Handy code lives in [api/handy_controller.py](../api/handy_controller.py); tests in
[tests/test_handy_controller.py](../tests/test_handy_controller.py).

---

## 1. Why the proxy has to drive the toy

Stash's interactive sync is implemented **entirely in its web player's browser JavaScript**
(`ScenePlayer/interactive.ts`), driven by HTML5 `<video>` events. There is **no GraphQL mutation**
that says "play scene X on the Handy synced to time T." Stash's server role is limited to storing the
Handy connection key + funscript offset, and serving the funscript file.

So to drive a toy for a *Jellyfin* client, the proxy must replicate what Stash's browser does, but
driven by the Jellyfin client's playback-reporting events instead of HTML5 video events. The enabler
already exists: the proxy receives `/sessions/playing`, `/sessions/playing/progress`, and
`/sessions/playing/stopped` ([api/userdata_routes.py](../api/userdata_routes.py)) carrying
`PlaySessionId`, `ItemId`, `PositionTicks`, and `IsPaused`. The Handy plays **autonomously** once told
"play at time T" (it keeps its own clock synced to the Handy server), so coarse (~5–10 s) progress
pings are enough — we only react to start / pause / resume / seek / stop.

## 2. Command transport — one path, cloud-relayed

**All** toy commands go through the **Handy cloud API** (`handyfeeling.com`), keyed by the device
**connection key** (`X-Connection-Key`). The Handy holds an outbound connection to the cloud; our
`PUT`/`GET`s are relayed down to the device by connection key. **No device IP is ever used** (it
changes on every device restart anyway).

> **"Local" ≠ LAN-only.** `HANDY_SYNC_MODE=local` / HSP does **not** bypass the cloud. Both HSSP and
> HSP relay through `handyfeeling.com`. HSP's real advantage over HSSP is that the script is streamed
> as ephemeral `{t,x}` data instead of being uploaded as a persistently cloud-hosted file (no hosted
> copy, no 512 KB cap) — not internet-bypass. A genuinely cloud-free path would require Bluetooth LE
> via Intiface/buttplug.io, a separate architecture that abandons this cloud sync engine.

## 3. API version & protocol selection

| Axis | Options | How chosen |
|---|---|---|
| **API version** | v2 (`…/handy/v2`, camelCase, `/mode`, `/connected`) · v3 (`…/handy-rest/v3`, snake_case, `/mode2`, `X-Api-Key`) | v3 iff `HANDY_APPLICATION_ID` is set (sent as `X-Api-Key`); else v2. Set per session in `HandyController.__init__` → `self.use_v3`. |
| **Serving protocol** | HSSP (cloud-hosted script file) · HSP (live point-streaming) | `_resolve_use_hsp(cfg)`: `HANDY_SYNC_MODE=hosted`→HSSP, `local`→HSP, `auto`→follows Stash's `useStashHostedFunscript`. |

**HSP requires v3.** If HSP is selected without `HANDY_APPLICATION_ID`, `_prepare()` **fails loudly**
(state → `failed`, one log line) rather than silently downshifting — beta problems stay visible.

`HANDY_APPLICATION_ID` is the Handy **ApplicationID** from devs.handyfeeling.com — a non-secret ID
that authenticates the non-privileged device endpoints via `X-Api-Key`. A backend like this proxy
never needs the secret ApplicationKey or the bearer-token flow (those are for untrusted browser
clients). The device connection key (`handyKey`) is still required per device.

## 4. Lifecycle

One `HandyController` per `PlaySessionId`. PlaybackInfo returns a deterministic
`PlaySessionId = stash_<scene_id>` that `/sessions/playing` echoes, which lets us pre-instantiate the
controller on PlaybackInfo and move ~1.7 s of setup **off** the play critical path.

```
PlaybackInfo ──prewarm(scene)──► preactivate() ─► _prepare()  (connect, offset, mode, setup) → state=ready
                                       │                          (device set up + clock-synced, NOT moving)
                                       └─ arms PREACTIVATION_ABANDON_S (45 s) watchdog
first /playing ─► begin_playback() ─► (prepare inline if still cold) ─► _play(resolved_pos)  → device moves
/progress ──────► on_progress()   ─► pause→_stop · resume→debounced _play · seek→debounced _play
/stopped ───────► teardown()      ─► _stop + cancel debounce/refill/watchdog · state=closed
```

Key methods (all in `HandyController`):

- **`_prepare()`** — read Stash config (`handyKey`, `funscriptOffset`, `useStashHostedFunscript`),
  probe `_get_connected()`, estimate the clock offset, then branch: `_prepare_hssp()` or
  `_prepare_hsp()`. Sets `state` to `ready` | `failed`.
- **`preactivate()`** — runs `_prepare()` on PlaybackInfo without playing; arms the abandonment
  watchdog so a browse-but-don't-play tears down cleanly.
- **`begin_playback(pos, is_paused)`** — first `/playing`; prepares inline if not pre-activated, then
  plays (unless paused) at `_resolve_initial_position(pos)`.
- **`_resolve_initial_position()`** priority: real reported pos (>1 s) → stream request's
  `startTimeTicks` (`note_start_position`) → Stash `resume_time` → reported. Avoids the `play@0` lurch
  on resumed scenes.
- **`on_progress(pos, is_paused)`** — pause → `_stop`; resume → debounced `_play`; seek (inferred when
  `|Δpos − Δwallclock| > SEEK_THRESHOLD_S` = 2 s) → debounced `_play`. The debounce
  (`_schedule_play`/`_debounced_play`, `SEEK_DEBOUNCE_S` = 0.4 s) coalesces a scrub burst into one
  play at the settled position.
- **`teardown()`** — stop + cancel pending play / refill task / watchdog; `state=closed`.

## 5. Timing & sync math

Both HSSP and HSP use the same synced-play contract: we tell the device *"at server-time `server_time`,
be at script position `start_time`,"* and it free-runs from there against the Handy-server clock.

```
start_time  = round(position_seconds * 1000 + script_offset_ms)   # ms into the script
server_time = round(cs_offset + now_ms)                           # Tcest — "now" on the Handy clock
```

- **`cs_offset`** (client↔Handy-server clock delta) is the mean of `(server_time_sample + rtt/2 − recv)`
  over `OFFSET_SAMPLES` (5) `/servertime` samples. This bakes in **one-way** (`rtt/2`) latency to the
  Handy server. Bench `cs_offset` is typically −3…+5 ms, so clock sync is tight.
- **`script_offset_ms`** = Stash's `funscriptOffset`, applied additively — the manual calibration knob
  (positive pulls the script *ahead* of the video).

**Position extrapolation (`_extrapolated_pos`).** The reported video position was sampled by the
client a moment before we issue the command (the seek debounce + processing). Since playback is 1×, we
advance the position by the wall time elapsed since it was sampled (`_last_event_t`), so the device
syncs to where the video is at *issue* time, not sample time. Applied once in `_play()` (covers HSSP
and HSP); bounded by `MAX_EXTRAPOLATION_S` (2 s) so a stale timestamp can't overshoot. This cancels the
report→issue lag — mostly the 0.4 s seek debounce. It does **not** correct the client→proxy network leg
(small on LAN) or the device's physical actuation latency; those residuals are what `funscriptOffset`
trims.

## 6. HSSP path (cloud-hosted script)

`_prepare_hssp()`:
1. `_prepare_upload_url(scene_id, funscript_url)` — fetch the funscript from Stash (with our API key),
   convert to Handy CSV (`funscript_to_csv`: `at,pos\r\n` rows, honors `inverted`, clamps 0–100), POST
   to `…/api/sync/upload?local=true`, get a content-hash hosting URL back. Cached per scene
   (`SCRIPT_URL_TTL_S` = 1 h); `prewarm()` populates it eagerly on PlaybackInfo.
2. `_set_mode(MODE_HSSP)` (`/mode2` on v3, `/mode` on v2).
3. `_hssp_setup(url)` → `hssp/setup {url}`; then a `SETUP_SETTLE_S` (0.25 s) wait for the device to
   download the script.

Play/stop: `_play` → `hssp/play {start_time, server_time}` (snake_case v3 / camelCase v2); `_stop` →
`hssp/stop`.

> HSSP on FW 4.2.x **only** accepts publicly-hosted (cloud) URLs — private-network URLs return
> `HTTP 400 UNSUPPORTED_URL` (for Stash too). The LAN-serve endpoint
> `GET /handy/scene/{scene_id}/funscript` ([routes.py](../routes.py)) is retained for a future
> publicly-reachable deployment but is **not** used by the current HSSP path.

## 7. HSP path (live point-streaming, v3-only)

`_prepare_hsp()`: fetch the funscript, convert to `{t,x}` points (`funscript_to_points` — same
inverted/clamp logic as CSV, sorted by `t`), `_set_mode(MODE_HSP)` (`/mode2 {mode:4}`), then
`_hsp_setup()` (`hsp/setup`, captures `max_points` ≈ 9936 and `stream_id`). No upload, no hosted file.

**Endpoints** (base `…/handy-rest/v3`):

| Endpoint | Purpose |
|---|---|
| `PUT /mode2 {mode:4}` | Enter HSP mode. |
| `PUT /hsp/setup {stream_id?}` | Start/clear an HSP session; returns `{result: HspState}`. |
| `PUT /hsp/add` | Add ≤100 points. Body: `{points:[{t,x}…], flush?, tail_point_stream_index (REQUIRED)}`. `flush:true` clears the buffer (used on seek). |
| `PUT /hsp/play {start_time, server_time, add?}` | Begins synced playback; an embedded `add` seeds + plays in one call. |
| `PUT /hsp/stop` | Stop. |
| `GET /hsp/state` | `HspState`: `points`, `max_points`, `current_point`, `current_time`, `tail_point_stream_index`, … |

**Point format:** `{t, x}` — `t` = int ms relative to `t=0` (script start), `x` = int slider position
**0–100** (bench-confirmed; the schema's `maximum:50` is a spec quirk the device ignores).

**`tail_point_stream_index`** is **run-relative and resets on flush** (`_hsp_add_body`): after a
`flush` the device clears its buffer and `current_point` resets to −1, so the counter restarts at
`len(batch)-1`; subsequent non-flush adds continue it (bench-confirmed the device echoes 99 → 199 →
299 → 399). It is *not* a session-wide counter.

### Buffer model (time-based)

The buffer is measured in **seconds of motion**, not point count, so the safety margin is identical
whether a script is sparse or frantic (400 points is ~40 s dense vs ~130 s sparse — a point count is
an accidental margin; seconds is a deliberate one). Three user-tunable knobs (defaults in parens):

- **`HANDY_HSP_BUFFER_MIN_S`** (30) — seconds seeded on play/seek. `_hsp_play` embeds the first ≤100
  points (`HSP_ADD_BATCH`) in the `hsp/play` flush so motion starts instantly, then `_hsp_fill_to`
  streams follow-up `hsp/add`s until the buffer covers `start_time + MIN` ms.
- **`HANDY_HSP_BUFFER_MAX_S`** (60) — the refill target. `_hsp_refill_loop` wakes every poll interval,
  reads the device's `current_time` (`_hsp_current_time`), and `_hsp_fill_to(current_time + MAX*1000)`
  tops the buffer up in ≤100-point chunks (capped `HSP_MAX_ADDS_PER_FILL` = 12 per pass, a rate-limit
  guard vs the 240 req/min device limit).
- **`HANDY_HSP_POLL_INTERVAL_S`** (15) — refill cadence.

Steady state floats ~45–60 s buffered; starving would need a two-poll (≥30 s) refill outage. The
refill task is started on first play, cancelled in `teardown`. On seek, `_hsp_play` flushes and
re-seeds; the loop resumes feeding forward from `_hsp_next_index`.

> **Design note — polling, not SSE.** The v3 API offers an SSE `hsp_starving` signal, but polling
> `/hsp/state` at 15 s is simpler and, given the large time buffer, more than sufficient. The 15 s
> cadence (vs an earlier 3 s point-count design) also cuts how often the refill task holds the
> per-controller lock ~5×, so a seek arriving mid-poll rarely waits — enough that a
> release-the-lock-during-IO refactor was judged unnecessary.

## 8. Isolation contract (mandatory)

The controller is a **best-effort, fully isolated side-channel**. It must never affect video playback:

- **Fire-and-forget fan-out.** `notify_playing` / `notify_stopped` / `prewarm` / `note_start_position`
  are synchronous schedulers that `asyncio.create_task(...)` a fully-guarded coroutine and return
  immediately, so Handy work can never delay the `/sessions/playing` 204 or touch playback.
- **Every Handy/network call is wrapped.** `_api_get`/`_api_put` catch network errors and return
  `None`; `_parse` treats both HTTP ≥400 and a `200 + {error}` envelope (the Handy returns 200 on
  rejected commands) as failure.
- **Probe once, fail once, no retries.** On any activation failure we log once and set `state=failed`;
  failed controllers are **retained** (not removed) so the next progress event doesn't re-probe.
  Teardown on `/stopped` removes the entry.
- **Locking.** A per-controller `asyncio.Lock` serializes prepare/play/progress/teardown; a module
  `_registry_lock` guards get-or-create of the controller registry.

## 9. Configuration

Proxy settings (config.py — all follow the standard default + `.conf` save + env override + GUI
pattern; wired in [templates/components/tab_settings.html](../templates/components/tab_settings.html)):

| Setting | Default | Purpose |
|---|---|---|
| `ENABLE_HANDY_SYNC` | `false` | Master toggle. |
| `HANDY_SYNC_MODE` | `auto` | `auto` \| `hosted` (force HSSP) \| `local` (force HSP). GUI kill-switch: flip to `hosted` for stable HSSP. |
| `HANDY_APPLICATION_ID` | `""` | v3 ApplicationID (`X-Api-Key`). Set → v3; blank → v2. Required for HSP. |
| `HANDY_HSP_BUFFER_MIN_S` | `30` | HSP seed floor (s). *Advanced.* |
| `HANDY_HSP_BUFFER_MAX_S` | `60` | HSP refill target (s). *Advanced.* |
| `HANDY_HSP_POLL_INTERVAL_S` | `15` | HSP refill poll interval (s). *Advanced.* |

The `HANDY_HSP_*` knobs are hidden behind an **Advanced options** disclosure in the Handy settings card
(HSP-only — HSSP ignores them). `_cfg_int` reads them live, so GUI changes take effect on the next
poll/play without a restart.

Read from **Stash** (via `get_stash_interface_config()`, `configuration.interface`, no cache):
`handyKey` (device connection key), `funscriptOffset` (ms), `useStashHostedFunscript` (HSSP vs HSP in
`auto` mode). These are intentionally **not** duplicated into the proxy config.

## 10. Code map

| File | Role |
|---|---|
| [api/handy_controller.py](../api/handy_controller.py) | Everything: `HandyController` lifecycle, HSSP + HSP primitives, Handy API client, funscript fetch/convert, fire-and-forget fan-out, LAN-serve endpoint. |
| [api/userdata_routes.py](../api/userdata_routes.py) | `endpoint_sessions_playing` → `notify_playing`; `endpoint_sessions_stopped` → `notify_stopped`. |
| [api/stream_routes.py](../api/stream_routes.py) | PlaybackInfo → `prewarm(scene)`; stream request `startTimeTicks` → `note_start_position`. |
| [core/stash_client.py](../core/stash_client.py) | `interactive` + `paths.funscript` on scene fields; `get_stash_interface_config()`. |
| [routes.py](../routes.py) | `GET /handy/scene/{scene_id}/funscript` (LAN-serve; retained, not on the HSSP path). |
| [config.py](../config.py) | The six `ENABLE_HANDY_SYNC` / `HANDY_*` settings. |
| [templates/components/tab_settings.html](../templates/components/tab_settings.html) | Handy settings card + Advanced options. |

## 11. Wire reference

**v2** (`https://www.handyfeeling.com/api/handy/v2`, header `X-Connection-Key`, **camelCase**):
`GET /connected`, `GET /servertime` (→ `{serverTime}`), `PUT /mode {mode:1}`, `PUT /hssp/setup {url}`,
`PUT /hssp/play {startTime, serverTime}`, `PUT /hssp/stop`. Success envelope `{result: 0}`.

**v3** (`https://www.handyfeeling.com/api/handy-rest/v3`, headers `X-Connection-Key` + `X-Api-Key`,
**snake_case**): `GET /connected` (→ `{result:{connected}}`), `GET /servertime` (→ `{server_time}`,
public — works without `X-Api-Key`), `PUT /mode2 {mode}` (HAMP 0, HSSP 1, HDSP 2, MAINTENANCE 3,
HSP 4), HSSP `hssp/setup|play|stop`, HSP `hsp/setup|add|play|stop|state`. Envelope: `{result}` on
success, `{error:{code,name,message,connected}}` on rejection (device endpoints return **HTTP 200**
even on rejection — a 200 alone is not success; `_parse` checks the envelope).

## 12. Testing

- [tests/test_handy_controller.py](../tests/test_handy_controller.py) — no live network; every Handy /
  handyfeeling / Stash call is mocked. Covers funscript→CSV/points conversion, event→command mapping
  (play/pause/resume/seek/scrub-coalesce/steady-state), sync-offset + position extrapolation,
  activation success/failure, fan-out gating + isolation, protocol/API-version selection, the HSP
  play/seed/refill/teardown flow, and the LAN-serve endpoint.
- `TestHandyFanOut` in [tests/test_userdata_routes.py](../tests/test_userdata_routes.py) — route-level
  fan-out.

## 13. Not yet done

- **Periodic drift re-sync per progress ping** — compare device expected position vs reported and
  re-sync only past a threshold. Currently we sync on start/seek/resume and rely on the device's
  autonomous clock (tight `cs_offset` makes drift small).
- **Manual offset knob in the proxy GUI** — the systematic extrapolation (§5) handles the report→issue
  lag; a per-deployment manual offset slider (beyond Stash's `funscriptOffset`) could dial in residual
  device-actuation latency. Not yet needed.
- **True cloud-free (Bluetooth LE / Intiface) transport** — separate architecture (§2); not planned.
