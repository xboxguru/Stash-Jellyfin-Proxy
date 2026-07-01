"""Feature 3 — Interactive Toy (Handy) Sync.

Drives one or more connected Handy devices in sync with an interactive scene's funscript, using the
Jellyfin playback-reporting events the proxy already receives (/sessions/playing[/progress],
/sessions/playing/stopped in api/userdata_routes.py). One `HandyController` per device; a
`HandySessionGroup` (keyed by PlaySessionId) fans lifecycle events out to all enabled devices. Device
registry lives in api/handy_devices.py.

Handy REST API. Uses **v3** (handy-rest/v3) when `HANDY_APPLICATION_ID` is configured — sent as the
`X-Api-Key` header — otherwise falls back to **v2** (device connection key only, as Stash uses).
Lifecycle:
    connect probe -> estimate server-time offset -> mode(HSSP) -> hssp/setup(url)
    -> hssp/play(start_time, server_time) / hssp/stop

Protocol is chosen per device (see `_resolve_use_hsp`, driven by the device's sync_mode):
  - HSSP (cloud-hosted script URL) — uploads the funscript to handyfeeling hosting.
  - HSP  (local point-streaming, v3-only) — streams the funscript as live {t,x} points instead of a
    hosted file: no persistent cloud copy, no 512 KB cap. NOTE: commands are still relayed through the
    Handy cloud (handyfeeling.com) to reach the device; "local" means the script isn't hosted as a
    file, NOT a device-LAN-only path (there is no LAN-direct path on FW 4.2.x).

Full design/reference: docs/handy_integration.md.

ISOLATION CONTRACT (mandatory — see docs/handy_integration.md §8):
The controller is a best-effort, fully isolated side-channel. Every public entry point is a
fire-and-forget scheduler and every Handy/network call is wrapped so an exception can NEVER reach
the /sessions/playing response or affect video playback. On any activation failure we log once,
mark the session's controller failed, and stop touching it — no retries.
"""

import asyncio
import bisect
import logging
import time
from typing import Any, Dict, Optional

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

import config
from api import handy_devices
from core import stash_client

logger = logging.getLogger(__name__)

# Handy REST API. v3 (handy-rest/v3) needs an ApplicationID (sent as X-Api-Key) on device
# endpoints; when `HANDY_APPLICATION_ID` is configured we use v3, otherwise we fall back to v2
# (which authenticates with only the device connection key — the same thing Stash uses). HSP
# (local streaming) is v3-only. See docs/handy_integration.md §3.
HANDY_API_BASE_V2 = "https://www.handyfeeling.com/api/handy/v2"
HANDY_API_BASE_V3 = "https://www.handyfeeling.com/api/handy-rest/v3"
# handyfeeling hosting upload for HSSP. Returns a content-hash cloud download URL. The old
# `?local=true` query flag was a guess at enabling LAN/local serving and was never confirmed to do
# anything — dropped; verify HSSP still uploads/plays on the bench.
HANDY_UPLOAD_URL = "https://www.handyfeeling.com/api/sync/upload"

# Device mode enum: HAMP=0, HSSP=1, HDSP=2, MAINTENANCE=3 (v3 also adds HSP=4).
MODE_HSSP = 1
MODE_HSP = 4


def _app_id() -> str:
    """The configured Handy REST v3 ApplicationID (X-Api-Key), or '' to use the legacy v2 API."""
    return str(getattr(config, "HANDY_APPLICATION_ID", "") or "").strip()

# A reported jump in position that diverges from wall-clock by more than this (seconds) is a seek.
SEEK_THRESHOLD_S = 2.0
# Coalesce rapid scrubbing: wait this long after the last position change before commanding the
# device, so a burst of seek pings collapses into a single play at the settled position.
SEEK_DEBOUNCE_S = 0.4
# When we issue a play, the reported video position was sampled a moment earlier (the debounce wait +
# processing). We advance the position by that elapsed wall time (scaled by the playback rate) so the
# device syncs to where the video is *now*, not where it was sampled — cancels the report→issue lag.
# Capped so a stale/anomalous timestamp can't overshoot.
MAX_EXTRAPOLATION_S = 2.0

# Playback-speed inference. Most clients don't report speed, so we derive it from how fast the
# reported position advances vs wall-clock (bench-confirmed accurate). A plausible in-range ratio is
# a rate sample; anything outside RATE_MIN..RATE_MAX is a seek/discontinuity. A rate *change* is only
# committed after two consecutive agreeing samples, so a one-off in-range seek isn't mistaken for it.
RATE_MIN, RATE_MAX = 0.1, 4.0
RATE_DEADBAND = 0.2          # rates within this of each other count as "the same"
MIN_RATE_SAMPLE_S = 1.5      # need at least this much wall gap between pings to estimate a rate
# Jellyfin exposes a fixed set of playback speeds. Inference is only accurate to ~±0.1, so we snap the
# committed rate to the nearest standard speed when close — otherwise a value like 1.42 (for a real
# 1.5x) locks in and drifts ~0.08x forever (per-ping residual stays under the seek threshold, so it's
# never re-corrected). A genuinely non-standard rate (outside the tolerance) is used as-is.
STANDARD_RATES = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
RATE_SNAP_TOLERANCE = 0.15
# Number of /servertime samples used to estimate the client<->Handy clock offset (cs_offset).
OFFSET_SAMPLES = 5
# Grace period after hssp/setup for the device to download the script before the first play.
SETUP_SETTLE_S = 0.25
# How long a prepared (uploaded) funscript URL stays reusable. The handyfeeling host URL is a
# content hash, so the same script always maps to the same URL — caching is safe.
SCRIPT_URL_TTL_S = 3600
# If a session is pre-activated (PlaybackInfo) but never actually plays within this window, tear the
# controller down so a browse-but-don't-play doesn't leave the device claimed.
PREACTIVATION_ABANDON_S = 45
# Upper bound on a single per-device lifecycle op (prepare/play/teardown) inside a session group's
# fan-out. Bounds the group lock a slow/black-holing device can hold: a legit prepare is ~2 s (connect
# + 5×servertime + setup), but a device that connects then hangs could otherwise stall the whole
# session (each network call has the 10 s HTTP timeout). On timeout we mark that device failed and
# leave the others untouched.
DEVICE_OP_TIMEOUT_S = 8.0

# --- HSP (local point-streaming) buffer tuning (Phase B) ------------------------
# The HSP buffer is measured in *seconds of motion*, not point count, so the safety margin is the
# same whether a script is sparse or frantic. On play/seek we seed HANDY_HSP_BUFFER_MIN_S seconds;
# a background task then polls every HANDY_HSP_POLL_INTERVAL_S and tops the buffer up to
# HANDY_HSP_BUFFER_MAX_S seconds ahead of the play head. Defaults below; all three are user-tunable
# via config / the GUI Advanced options (HSP path only — HSSP ignores them).
DEFAULT_HSP_BUFFER_MIN_S = 30
DEFAULT_HSP_BUFFER_MAX_S = 60
DEFAULT_HSP_POLL_INTERVAL_S = 15
# Device hard cap is 100 points per hsp/add call.
HSP_ADD_BATCH = 100
# Safety cap on hsp/add calls in a single fill pass, so a pathologically dense script can't burst
# past the 240 req/min device rate limit. 12 * 100 pts is far more than any sane seed/refill needs.
HSP_MAX_ADDS_PER_FILL = 12

# Registry of live session groups, keyed by PlaySessionId. Each group fans out to one controller per
# configured Handy device (multi-device — see handy_devices.py / docs §9a).
_groups: Dict[str, "HandySessionGroup"] = {}
_registry_lock = asyncio.Lock()

# Prepared upload-mode script URLs, keyed by scene_id -> (expiry_monotonic, url). Populated either
# lazily on activation or eagerly by prewarm() when PlaybackInfo is requested.
_script_url_cache: Dict[str, "tuple[float, str]"] = {}

# Exact start positions (seconds) captured from the stream request's startTimeTicks, keyed by
# PlaySessionId (stash_<scene_id>). Authoritative for the first play; 0 = play-from-beginning.
_start_pos_by_session: Dict[str, float] = {}

_http_client: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=10.0)
    return _http_client


def _now_ms() -> float:
    return time.time() * 1000.0


def _snap_rate(rate: float) -> float:
    """Snap an inferred playback rate to the nearest standard Jellyfin speed when within tolerance;
    otherwise return it rounded (a genuinely non-standard speed)."""
    nearest = min(STANDARD_RATES, key=lambda s: abs(s - rate))
    return nearest if abs(nearest - rate) <= RATE_SNAP_TOLERANCE else round(rate, 2)


def _resolve_use_hsp(sync_mode: str, cfg: Dict[str, Any]) -> bool:
    """Decide whether a device uses HSP (local streaming) vs HSSP (cloud hosting) from its per-device
    `sync_mode`: `hosted` forces HSSP, `local` forces HSP, `auto` follows Stash's
    `useStashHostedFunscript` (local serving -> HSP)."""
    mode = str(sync_mode or "auto").strip().lower()
    if mode == "hosted":
        return False
    if mode == "local":
        return True
    return bool(cfg.get("useStashHostedFunscript"))


async def _fetch_funscript(funscript_url: Optional[str]) -> Optional[Dict[str, Any]]:
    """GET a funscript JSON from Stash, sending our API key server-side so the device never needs
    the Stash key. Returns the parsed JSON or None on any failure."""
    if not funscript_url:
        return None
    headers = {}
    api_key = getattr(config, "STASH_API_KEY", "")
    if api_key:
        headers["ApiKey"] = api_key
    try:
        resp = await _client().get(funscript_url, headers=headers)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"[handy] funscript fetch failed ({funscript_url}): {e}")
        return None


async def _upload_csv(csv: str, scene_id: Optional[str]) -> Optional[str]:
    """POST a CSV funscript to handyfeeling's host and return the device-reachable URL. The URL is
    a content hash, so re-uploading identical content returns the same URL."""
    try:
        files = {"file": ("script.csv", csv.encode("utf-8"), "text/csv")}
        resp = await _client().post(HANDY_UPLOAD_URL, files=files)
        resp.raise_for_status()
        data = resp.json()
        logger.debug(f"[handy] upload response ({len(csv)} bytes sent): {str(data)[:300]}")
        url = data.get("url") or (data.get("data") or {}).get("url")
        if not url:
            logger.warning(f"[handy] upload returned no URL; response keys={list(data.keys()) if isinstance(data, dict) else type(data)}")
        return url
    except Exception as e:
        logger.warning(f"[handy] funscript upload failed for scene {scene_id}: {e}")
        return None


async def _prepare_upload_url(scene_id: Optional[str], funscript_url: Optional[str]) -> Optional[str]:
    """Return a handyfeeling-hosted URL for the scene's funscript, using the cache when fresh.
    Otherwise fetch from Stash, convert to CSV, upload, and cache the result."""
    now = time.monotonic()
    if scene_id:
        cached = _script_url_cache.get(scene_id)
        if cached and cached[0] > now:
            logger.debug(f"[handy] reusing prepared funscript URL for scene {scene_id}")
            return cached[1]
    funscript = await _fetch_funscript(funscript_url)
    if not funscript:
        return None
    csv = funscript_to_csv(funscript)
    url = await _upload_csv(csv, scene_id)
    if url and scene_id:
        _script_url_cache[scene_id] = (now + SCRIPT_URL_TTL_S, url)
    return url


def funscript_to_csv(funscript: Dict[str, Any]) -> str:
    """Convert a funscript's actions to the Handy CSV format (`at,pos\\r\\n`).

    Mirrors Stash interactive.ts's `at,pos` rows; additionally honours a top-level `inverted`
    flag if the script carries one (harmless when absent). Range/intensity remapping is left to
    the device's own slide-min/max settings, matching Stash's behaviour.
    """
    actions = funscript.get("actions") or []
    inverted = bool(funscript.get("inverted", False))
    rows = []
    for a in actions:
        try:
            at = int(a.get("at", 0))
            pos = int(a.get("pos", 0))
        except (TypeError, ValueError):
            continue
        if inverted:
            pos = 100 - pos
        pos = max(0, min(100, pos))
        rows.append(f"{at},{pos}")
    return "\r\n".join(rows) + "\r\n"


def funscript_to_points(funscript: Dict[str, Any]) -> "list[Dict[str, int]]":
    """Convert a funscript's actions to HSP stream points: `{t: <ms from t=0>, x: <pos 0–100>}`.

    Same conversion as funscript_to_csv (inverted flag + clamp), but emits {t,x} dicts to push
    straight into the device buffer — no CSV/upload, no hosted-file copy. Sorted by t
    so the buffer window / seek bisect can rely on ordering.

    Clamped to 0–100 to match HSSP/CSV. (The HSP schema tags `x` with `maximum: 50`, but that's a
    spec quirk — bench-confirmed on FW 4.2.2 that the device accepts and moves across the full 0–100.)
    """
    actions = funscript.get("actions") or []
    inverted = bool(funscript.get("inverted", False))
    points: "list[Dict[str, int]]" = []
    for a in actions:
        try:
            at = int(a.get("at", 0))
            pos = int(a.get("pos", 0))
        except (TypeError, ValueError):
            continue
        if inverted:
            pos = 100 - pos
        pos = max(0, min(100, pos))
        points.append({"t": at, "x": pos})
    points.sort(key=lambda p: p["t"])
    return points


class HandyController:
    """Drives one Handy device for one PlaySessionId. All public methods are serialized by
    `self.lock`; state transitions: init -> ready | failed -> closed."""

    def __init__(self, session_id: str, scene: Dict[str, Any], device: Optional[Dict[str, Any]] = None):
        self.session_id = session_id
        self.scene_id = scene.get("id")
        self.stash_funscript_url = (scene.get("paths") or {}).get("funscript")
        try:
            self.resume_time_s = float(scene.get("resume_time") or 0)
        except (TypeError, ValueError):
            self.resume_time_s = 0.0
        self.lock = asyncio.Lock()
        self.state = "init"  # init | ready | failed | closed

        # Per-device binding (multi-device — see handy_devices.py / docs §9a). When no device is given
        # (standalone/legacy), key falls back to Stash's handyKey in _prepare and mode to the global
        # HANDY_SYNC_MODE. The ApplicationID is application-level and always global.
        self.device = device or {}
        self.label: str = self.device.get("label") or "Handy"
        self.key: str = (self.device.get("key") or "").strip()
        self.sync_mode: str = str(self.device.get("sync_mode") or getattr(config, "HANDY_SYNC_MODE", "auto")).strip().lower()
        self._dev_offset_ms = self.device.get("funscript_offset")   # None -> use Stash funscriptOffset
        self._dev_hsp_min = self.device.get("hsp_buffer_min_s")     # None -> global HANDY_HSP_* default
        self._dev_hsp_max = self.device.get("hsp_buffer_max_s")
        self._dev_hsp_poll = self.device.get("hsp_poll_interval_s")

        self.script_offset_ms: int = 0
        self.estimated_offset_ms: float = 0.0  # cs_offset
        self.app_id: str = _app_id()
        self.use_v3: bool = bool(self.app_id)
        self.use_hsp: bool = False  # resolved in _prepare(); HSP (local streaming) vs HSSP

        # HSP streaming state (Phase B). Populated only when use_hsp is True.
        self._hsp_points: "list[Dict[str, int]]" = []   # full {t,x} stream, sorted by t
        self._hsp_times: "list[int]" = []               # parallel list of t's for seek bisect
        self._hsp_next_index: int = 0                   # next point to push (forward streaming)
        self._hsp_points_sent: int = 0                  # cumulative points in the current buffer run
        self._hsp_max_points: int = 0                   # device buffer cap reported by hsp/setup
        self._hsp_stream_id: Optional[int] = None
        self._hsp_refill_task: Optional[asyncio.Task] = None

        self._is_playing = False
        self._playback_started = False   # True once the first play event has been handled
        self._playback_rate: float = 1.0                 # inferred client playback speed
        self._rate_candidate: Optional[float] = None     # unconfirmed pending rate (needs 2nd sample)
        self._last_position_s: Optional[float] = None
        self._last_event_t: Optional[float] = None
        self._pending_play_pos: Optional[float] = None
        self._pending_play_task: Optional[asyncio.Task] = None
        self._abandon_task: Optional[asyncio.Task] = None

    # --- lifecycle -------------------------------------------------------

    async def _prepare(self):
        """Do everything up to (but not including) playback: read config, probe connect, estimate
        the clock offset, prepare the script, set HSSP mode + setup. Sets state to ready|failed.
        This is the ~1.7 s of work we move off the critical path via preactivate()."""
        try:
            cfg = await stash_client.get_stash_interface_config()
            # Connection key: the bound device's key, else fall back to Stash's handyKey (legacy /
            # standalone). Offset: per-device override wins, else Stash's funscriptOffset.
            if not self.key:
                self.key = (cfg.get("handyKey") or "").strip()
            try:
                self.script_offset_ms = int(self._dev_offset_ms if self._dev_offset_ms is not None
                                            else (cfg.get("funscriptOffset") or 0))
            except (TypeError, ValueError):
                self.script_offset_ms = 0
            use_hsp = _resolve_use_hsp(self.sync_mode, cfg)
            self.use_hsp = use_hsp
            logger.debug(
                f"[handy] preparing session={self.session_id} dev={self.label} scene={self.scene_id} "
                f"api={'v3' if self.use_v3 else 'v2'} key=...{(self.key[-4:] if self.key else '----')} "
                f"app_id={'set' if self.app_id else 'none'} script_offset={self.script_offset_ms}ms "
                f"sync_mode={self.sync_mode} use_hsp={use_hsp}"
            )

            if not self.key:
                logger.info(f"[handy] no connection key for device {self.label}; disabling sync for session {self.session_id}")
                self.state = "failed"
                return

            # HSP (local streaming) is v3-only. Fail loudly rather than silently down-shifting to
            # HSSP so the mis-config (HSP selected without HANDY_APPLICATION_ID) is visible.
            if use_hsp and not self.use_v3:
                logger.warning(
                    f"[handy] HSP (local streaming) requires the v3 API (set HANDY_APPLICATION_ID); "
                    f"disabling sync for dev={self.label} session {self.session_id}"
                )
                self.state = "failed"
                return

            if not await self._get_connected():
                logger.info(f"[handy] device not connected (dev={self.label} key ...{self.key[-4:]}); disabling sync for session {self.session_id}")
                self.state = "failed"
                return

            self.estimated_offset_ms = await self._estimate_offset()
            if self.estimated_offset_ms == 0.0:
                logger.warning(
                    f"[handy] server-time offset estimated as 0 for session {self.session_id} — "
                    f"all /servertime samples failed to parse; sync timing will be unreliable"
                )

            if use_hsp:
                if not await self._prepare_hsp():
                    self.state = "failed"
                    return
            else:
                if not await self._prepare_hssp():
                    self.state = "failed"
                    return

            # Give the device a moment to settle (download script / open session) before first play.
            await asyncio.sleep(SETUP_SETTLE_S)

            self.state = "ready"
            proto = "HSP" if use_hsp else "HSSP"
            logger.info(
                f"[handy] prepared ({proto} {'v3' if self.use_v3 else 'v2'}) dev={self.label} session={self.session_id} "
                f"scene={self.scene_id} cs_offset={self.estimated_offset_ms:.0f}ms "
                f"script_offset={self.script_offset_ms}ms"
                + (f" points={len(self._hsp_points)} max_points={self._hsp_max_points}" if use_hsp else "")
            )
        except Exception as e:
            logger.warning(f"[handy] prepare failed for session {self.session_id}: {e}")
            self.state = "failed"

    async def _prepare_hssp(self) -> bool:
        """HSSP setup: hosted script URL + mode(HSSP) + hssp/setup. Returns False on any failure."""
        # HSSP requires a publicly-hosted script URL (private URLs rejected on FW 4.2.x).
        script_url = await _prepare_upload_url(self.scene_id, self.stash_funscript_url)
        if not script_url:
            logger.info(f"[handy] funscript unavailable for scene {self.scene_id}; disabling sync for session {self.session_id}")
            return False
        if not await self._set_mode(MODE_HSSP):
            logger.info(f"[handy] could not set HSSP mode for session {self.session_id}; disabling sync")
            return False
        if not await self._hssp_setup(script_url):
            logger.info(f"[handy] HSSP setup failed for scene {self.scene_id}; disabling sync for session {self.session_id}")
            return False
        return True

    async def _prepare_hsp(self) -> bool:
        """HSP setup: fetch the funscript, convert to {t,x} points (streamed live, not hosted), mode(HSP) +
        hsp/setup to open a streaming session. The buffer is seeded lazily at first play from the
        play position (see _hsp_play). Returns False on any failure."""
        funscript = await _fetch_funscript(self.stash_funscript_url)
        if not funscript:
            logger.info(f"[handy] funscript unavailable for scene {self.scene_id}; disabling sync for session {self.session_id}")
            return False
        self._hsp_points = funscript_to_points(funscript)
        if not self._hsp_points:
            logger.info(f"[handy] funscript has no usable actions for scene {self.scene_id}; disabling sync for session {self.session_id}")
            return False
        self._hsp_times = [p["t"] for p in self._hsp_points]
        if not await self._set_mode(MODE_HSP):
            logger.info(f"[handy] could not set HSP mode for session {self.session_id}; disabling sync")
            return False
        if not await self._hsp_setup():
            logger.info(f"[handy] HSP setup failed for scene {self.scene_id}; disabling sync for session {self.session_id}")
            return False
        return True

    async def preactivate(self, arm_watchdog: bool = True):
        """Pre-instantiate on PlaybackInfo: run _prepare() so the device is set up and the clock
        synced, but do NOT play (no motion until the user actually starts). Arms an abandonment
        timeout so a browse-but-don't-play leaves nothing lingering. When driven by a
        HandySessionGroup, `arm_watchdog=False` — the group owns abandonment for the whole session."""
        async with self.lock:
            if self.state != "init":
                return
            await self._prepare()
            if arm_watchdog and not self._playback_started:
                self._arm_abandon_timeout()

    async def begin_playback(self, position_seconds: float, is_paused: bool):
        """Handle the first /playing event. If not pre-activated, prepare now; otherwise the ~1.7 s
        of setup is already done and this is just the play command."""
        async with self.lock:
            if self.state == "init":
                await self._prepare()
            self._cancel_abandon_timeout()
            self._playback_started = True
            if self.state != "ready":
                return
            initial_pos = self._resolve_initial_position(position_seconds)
            self._last_position_s = initial_pos
            self._last_event_t = time.monotonic()
            logger.info(
                f"[handy] begin playback dev={self.label} session={self.session_id} @ {initial_pos:.1f}s "
                f"(reported {position_seconds:.1f}s) paused={is_paused}"
            )
            if not is_paused:
                await self._play(initial_pos)

    def _resolve_initial_position(self, reported_pos: float) -> float:
        """Pick the starting position: a real reported position wins; else the exact start from the
        stream request's startTimeTicks (authoritative — 0 means play-from-beginning); else Stash's
        resume point; else the reported ~0."""
        if reported_pos and reported_pos > 1.0:
            return reported_pos
        start = _start_pos_by_session.get(self.session_id)
        if start is not None:
            return start
        if self.resume_time_s > 1.0:
            logger.debug(f"[handy] using Stash resume {self.resume_time_s:.1f}s for session {self.session_id}")
            return self.resume_time_s
        return reported_pos

    # --- pre-activation abandonment watchdog ----------------------------

    def _arm_abandon_timeout(self):
        if self._playback_started:
            return
        self._abandon_task = asyncio.create_task(self._abandon_watchdog())

    def _cancel_abandon_timeout(self):
        if self._abandon_task and not self._abandon_task.done():
            self._abandon_task.cancel()
        self._abandon_task = None

    async def _abandon_watchdog(self):
        try:
            await asyncio.sleep(PREACTIVATION_ABANDON_S)
        except asyncio.CancelledError:
            return
        if self._playback_started:
            return
        logger.info(
            f"[handy] pre-activation abandoned (no playback in {PREACTIVATION_ABANDON_S}s) — "
            f"tearing down device {self.label} session={self.session_id}"
        )
        await self.teardown()

    async def on_progress(self, position_seconds: float, is_paused: bool):
        async with self.lock:
            if self.state != "ready":
                return
            try:
                now = time.monotonic()
                prev_pos = self._last_position_s
                prev_t = self._last_event_t
                self._last_position_s = position_seconds
                self._last_event_t = now

                if is_paused:
                    self._cancel_pending_play()
                    if self._is_playing:
                        await self._stop()
                    return

                # Not paused. Resume (or first play after a paused start) — debounced.
                if not self._is_playing:
                    self._schedule_play(position_seconds)
                    return

                # Playing. Two things per ping, both debounced (a scrub collapses to one re-play):
                #   1) infer the client playback rate from Δpos/Δwall (clients rarely report it);
                #   2) infer a seek when the position jumps beyond what the current rate explains.
                if prev_pos is not None and prev_t is not None:
                    d_pos = position_seconds - prev_pos
                    d_wall = now - prev_t
                    if self._update_rate_and_maybe_replay(d_pos, d_wall, position_seconds):
                        return
                    # Rate-aware seek: divergence from the expected (rate-scaled) advance.
                    if abs(d_pos - d_wall * self._playback_rate) > SEEK_THRESHOLD_S:
                        logger.info(
                            f"[handy] seek detected {prev_pos:.1f}->{position_seconds:.1f}s "
                            f"(Δpos={d_pos:.1f} Δwall={d_wall:.1f} rate={self._playback_rate}) -> "
                            f"re-play (debounced) dev={self.label} session={self.session_id}"
                        )
                        self._schedule_play(position_seconds)
            except Exception as e:
                logger.debug(f"[handy] progress handling error for session {self.session_id}: {e}")

    def _update_rate_and_maybe_replay(self, d_pos: float, d_wall: float, position_seconds: float) -> bool:
        """Feed one progress interval into the playback-rate estimator. Commits a rate change (and
        re-plays at the new rate) only after two consecutive agreeing off-rate samples, so a one-off
        in-range seek isn't mistaken for a speed change. Returns True if it issued the re-play (caller
        then skips its own seek check). Out-of-range advances are left for the caller's seek check.
        Caller holds self.lock."""
        if d_wall < MIN_RATE_SAMPLE_S:
            return False
        raw = d_pos / d_wall
        if not (RATE_MIN <= raw <= RATE_MAX):
            self._rate_candidate = None      # discontinuity (seek) — not a plausible speed
            return False
        if abs(raw - self._playback_rate) <= RATE_DEADBAND:
            self._rate_candidate = None      # steady at the current rate
            return False
        if self._rate_candidate is not None and abs(raw - self._rate_candidate) <= RATE_DEADBAND:
            self._playback_rate = _snap_rate(raw)   # settled sample, snapped to the nearest standard speed
            self._rate_candidate = None
            logger.info(f"[handy] playback rate -> {self._playback_rate}x dev={self.label} session={self.session_id}")
            self._schedule_play(position_seconds)
            return True
        self._rate_candidate = raw           # first off-rate sample (unconfirmed)
        return False

    # --- debounced play scheduling --------------------------------------

    def _cancel_pending_play(self):
        if self._pending_play_task and not self._pending_play_task.done():
            self._pending_play_task.cancel()
        self._pending_play_task = None

    def _schedule_play(self, position_seconds: float):
        """Coalesce rapid play requests: remember the latest target position and (re)arm a single
        delayed play. Caller holds self.lock; this does not block."""
        self._pending_play_pos = position_seconds
        self._cancel_pending_play()
        self._pending_play_task = asyncio.create_task(self._debounced_play())

    async def _debounced_play(self):
        try:
            await asyncio.sleep(SEEK_DEBOUNCE_S)
        except asyncio.CancelledError:
            return
        async with self.lock:
            if self.state != "ready" or self._pending_play_pos is None:
                return
            try:
                await self._play(self._pending_play_pos)
            except Exception as e:
                logger.debug(f"[handy] debounced play error for session {self.session_id}: {e}")

    async def teardown(self):
        async with self.lock:
            try:
                self._cancel_pending_play()
                self._cancel_abandon_timeout()
                self._cancel_refill()
                if self.state == "ready" and self._is_playing:
                    await self._stop()
            except Exception as e:
                logger.debug(f"[handy] teardown stop error for session {self.session_id}: {e}")
            finally:
                self.state = "closed"
                _start_pos_by_session.pop(self.session_id, None)
                logger.info(f"[handy] torn down dev={self.label} session={self.session_id} scene={self.scene_id}")

    # --- Handy command primitives (v3) ----------------------------------

    def _extrapolated_pos(self, position_seconds: float) -> float:
        """Advance the reported position by the wall time elapsed since it was sampled (scaled by the
        playback rate — at 2x the video moved 2 media-seconds per wall-second), so the play command
        reflects where the video is at *issue* time rather than at sample time. This cancels the
        report→issue delay (mainly the seek debounce). Does not correct the client→proxy network leg —
        that residual is left for a future manual offset knob. Bounded by MAX_EXTRAPOLATION_S."""
        if self._last_event_t is None:
            return position_seconds
        age = time.monotonic() - self._last_event_t
        if age <= 0:
            return position_seconds
        return position_seconds + min(age, MAX_EXTRAPOLATION_S) * self._playback_rate

    async def _play(self, position_seconds: float):
        position_seconds = self._extrapolated_pos(position_seconds)
        if self.use_hsp:
            await self._hsp_play(position_seconds)
            return
        # server_time = estimated offset + now (Tcest). v3 uses snake_case keys, v2 camelCase.
        start_time = round(position_seconds * 1000 + self.script_offset_ms)
        server_time = round(self.estimated_offset_ms + _now_ms())
        if self.use_v3:
            # playback_rate lets the device match non-1x video speed (v3 synced-play contract). At 1.0
            # it's a no-op, so it's always safe to send; v2 (legacy) has no rate field.
            body = {"start_time": start_time, "server_time": server_time, "playback_rate": self._playback_rate}
        else:
            body = {"startTime": start_time, "serverTime": server_time}
        result = await self._api_put("hssp/play", body)
        if result is None:
            logger.warning(f"[handy] play@{start_time}ms NOT accepted by dev={self.label}, session={self.session_id}")
            return
        self._is_playing = True
        logger.info(f"[handy] play@{start_time}ms (server_time={server_time}) dev={self.label} session={self.session_id}")

    async def _stop(self):
        await self._api_put("hsp/stop" if self.use_hsp else "hssp/stop", {})
        self._is_playing = False
        logger.info(f"[handy] stop dev={self.label} session={self.session_id}")

    async def _set_mode(self, mode_value: int) -> bool:
        # v3 uses /mode2 (needs the ApplicationID); v2 uses /mode.
        endpoint = "mode2" if self.use_v3 else "mode"
        result = await self._api_put(endpoint, {"mode": mode_value})
        return result is not None

    async def _hssp_setup(self, script_url: str) -> bool:
        result = await self._api_put("hssp/setup", {"url": script_url})
        return result is not None

    # --- HSP (local streaming) primitives (v3) --------------------------

    async def _hsp_setup(self) -> bool:
        """Open an HSP streaming session (clears any prior buffer). Captures the device's buffer
        cap / stream_id from the returned HspState."""
        result = await self._api_put("hsp/setup", {})
        if result is None:
            return False
        res = result.get("result") if isinstance(result, dict) else None
        if isinstance(res, dict):
            try:
                self._hsp_max_points = int(res.get("max_points") or 0)
            except (TypeError, ValueError):
                self._hsp_max_points = 0
            self._hsp_stream_id = res.get("stream_id")
        return True

    def _hsp_seed_index(self, start_time_ms: int) -> int:
        """First point index at or after start_time_ms — the point to begin streaming from on a
        play/seek. Points earlier than the play head are already in the past, so we skip them."""
        return bisect.bisect_left(self._hsp_times, start_time_ms)

    def _hsp_add_body(self, batch: "list[Dict[str, int]]", flush: bool) -> Dict[str, Any]:
        """Build an HspAdd body and advance the tail stream index. `tail_point_stream_index` is the
        absolute index of the last point in the buffer run. A flush starts a fresh run (the device
        clears its buffer and its current_point resets to -1/0), so we reset the counter; otherwise
        we continue it. Bench-confirmed: the device echoes exactly this run-relative index (99 after
        a flush, then 199/299/399 as refill adds land) — it is NOT a never-resetting session counter."""
        if flush:
            self._hsp_points_sent = len(batch)
        else:
            self._hsp_points_sent += len(batch)
        return {
            "points": batch,
            "flush": flush,
            "tail_point_stream_index": self._hsp_points_sent - 1,
        }

    async def _hsp_play(self, position_seconds: float):
        """Start synced playback at the play position and seed the buffer with the first
        HANDY_HSP_BUFFER_MIN_S seconds of motion. The initial <=100 points ride along in the hsp/play
        call (flush) so motion starts immediately; any remainder needed to reach the seed floor is
        streamed in follow-up hsp/add calls. Same sync math as HSSP: start_time is the script-ms to
        begin at, server_time is our clock estimate."""
        start_time = round(position_seconds * 1000 + self.script_offset_ms)
        server_time = round(self.estimated_offset_ms + _now_ms())
        idx = self._hsp_seed_index(start_time)
        batch = self._hsp_points[idx: idx + HSP_ADD_BATCH]
        if not batch:
            logger.info(
                f"[handy] hsp play@{start_time}ms past end of script "
                f"({len(self._hsp_points)} points); nothing to stream, session={self.session_id}"
            )
            return
        self._hsp_next_index = idx + len(batch)
        body = {
            "start_time": start_time,
            "server_time": server_time,
            "playback_rate": self._playback_rate,   # match non-1x video speed (1.0 = normal)
            "add": self._hsp_add_body(batch, flush=True),
        }
        result = await self._api_put("hsp/play", body)
        if result is None:
            logger.warning(f"[handy] hsp play@{start_time}ms NOT accepted by dev={self.label}, session={self.session_id}")
            return
        self._is_playing = True
        logger.info(
            f"[handy] hsp play@{start_time}ms (server_time={server_time}) dev={self.label} seeded {len(batch)} points "
            f"from index {idx} session={self.session_id}"
        )
        # Top the fresh buffer up to the seed floor (MIN seconds ahead of the play head) so we start
        # with the full safety margin rather than just the first 100 points, then start the refill.
        min_s = self._cfg_int("HANDY_HSP_BUFFER_MIN_S", DEFAULT_HSP_BUFFER_MIN_S, self._dev_hsp_min)
        await self._hsp_fill_to(start_time + min_s * 1000)
        self._start_refill()

    async def _hsp_fill_to(self, target_t_ms: int):
        """Stream forward points (non-flush) until the buffer covers up to target_t_ms, in <=100-point
        chunks and capped at HSP_MAX_ADDS_PER_FILL calls. No-op once the buffer already reaches
        target_t_ms or the whole script has been pushed. Caller holds self.lock."""
        end_idx = bisect.bisect_right(self._hsp_times, target_t_ms)
        adds = 0
        while self._hsp_next_index < end_idx and adds < HSP_MAX_ADDS_PER_FILL:
            batch = self._hsp_points[self._hsp_next_index: min(self._hsp_next_index + HSP_ADD_BATCH, end_idx)]
            result = await self._api_put("hsp/add", self._hsp_add_body(batch, flush=False))
            if result is None:
                break
            self._hsp_next_index += len(batch)
            adds += 1

    # --- HSP buffer refill task -----------------------------------------

    def _start_refill(self):
        """Ensure the per-session refill task is running (idempotent)."""
        if self._hsp_refill_task and not self._hsp_refill_task.done():
            return
        self._hsp_refill_task = asyncio.create_task(self._hsp_refill_loop())

    def _cancel_refill(self):
        if self._hsp_refill_task and not self._hsp_refill_task.done():
            self._hsp_refill_task.cancel()
        self._hsp_refill_task = None

    async def _hsp_refill_loop(self):
        """Every HANDY_HSP_POLL_INTERVAL_S, top the buffer back up to HANDY_HSP_BUFFER_MAX_S seconds
        ahead of the device's play head. Holds the lock only while refilling; skips while
        paused/stopped or once the whole script is streamed. A seek re-seeds (flush) via _hsp_play,
        so this just keeps feeding forward from _hsp_next_index."""
        try:
            while True:
                await asyncio.sleep(self._cfg_int("HANDY_HSP_POLL_INTERVAL_S", DEFAULT_HSP_POLL_INTERVAL_S, self._dev_hsp_poll))
                async with self.lock:
                    if self.state != "ready" or not self._is_playing:
                        continue
                    if self._hsp_next_index >= len(self._hsp_points):
                        continue  # entire script has been pushed
                    await self._hsp_refill_once()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(f"[handy] hsp refill loop error session={self.session_id}: {e}")

    async def _hsp_refill_once(self):
        """One refill pass (caller holds self.lock). Reads the device's current play time and streams
        forward points until the buffer covers up to HANDY_HSP_BUFFER_MAX_S seconds ahead of it."""
        state = await self._api_get("hsp/state")
        current_time = self._hsp_current_time(state)
        if current_time is None:
            return
        max_s = self._cfg_int("HANDY_HSP_BUFFER_MAX_S", DEFAULT_HSP_BUFFER_MAX_S, self._dev_hsp_max)
        await self._hsp_fill_to(current_time + max_s * 1000)

    @staticmethod
    def _hsp_current_time(state: Optional[Dict[str, Any]]) -> Optional[int]:
        """Extract the device's current play time (ms) from an /hsp/state response, or None."""
        if not isinstance(state, dict):
            return None
        res = state.get("result")
        src = res if isinstance(res, dict) else state
        try:
            return int(src["current_time"])
        except (KeyError, TypeError, ValueError):
            return None

    def _cfg_int(self, name: str, default: int, override: Optional[int] = None) -> int:
        """Resolve an int knob: per-device override wins, else the live global config value, else
        default. Read fresh each call so GUI edits take effect without a restart."""
        val = override if override is not None else getattr(config, name, default)
        try:
            return int(val if val is not None else default)
        except (TypeError, ValueError):
            return default

    async def _get_connected(self) -> bool:
        data = await self._api_get("connected")
        logger.debug(f"[handy] connected check raw: {data}")
        if not data:
            return False
        result = data.get("result")
        if isinstance(result, dict) and "connected" in result:
            return bool(result.get("connected"))
        # Tolerate a flat shape just in case.
        if "connected" in data:
            return bool(data.get("connected"))
        return False

    async def _estimate_offset(self) -> float:
        """Estimate client->server clock offset (cs_offset, ms) per the v3 algorithm:
        offset = (server_time + rtt/2) - receive_time, averaged over samples."""
        total = 0.0
        count = 0
        observed_keys = None
        for i in range(OFFSET_SAMPLES):
            send = _now_ms()
            data = await self._api_get("servertime")
            recv = _now_ms()
            if not data:
                continue
            if i == 0:
                logger.debug(f"[handy] /servertime raw first sample: {data}")
            # v2 returns {serverTime: <ms>} (camelCase); tolerate snake_case / result-wrapped too.
            server_time = data.get("serverTime")
            if server_time is None:
                server_time = data.get("server_time")
            if server_time is None and isinstance(data.get("result"), dict):
                server_time = data["result"].get("serverTime") or data["result"].get("server_time")
            if server_time is None:
                if observed_keys is None and isinstance(data, dict):
                    observed_keys = list(data.keys())
                continue
            rtt = recv - send
            estimated_now = float(server_time) + rtt / 2.0
            total += estimated_now - recv
            count += 1
        if count:
            offset = total / count
            logger.debug(f"[handy] cs_offset = {offset:.0f}ms from {count}/{OFFSET_SAMPLES} samples")
            return offset
        logger.warning(
            f"[handy] /servertime returned no parseable server_time in {OFFSET_SAMPLES} samples; "
            f"observed top-level keys={observed_keys}"
        )
        return 0.0

    # --- HTTP helpers ----------------------------------------------------

    def _api_base(self) -> str:
        return HANDY_API_BASE_V3 if self.use_v3 else HANDY_API_BASE_V2

    def _headers(self) -> Dict[str, str]:
        headers = {"X-Connection-Key": self.key, "Content-Type": "application/json"}
        if self.use_v3 and self.app_id:
            headers["X-Api-Key"] = self.app_id  # v3 ApplicationID
        return headers

    def _parse(self, method: str, path: str, resp, elapsed_ms: float) -> Optional[Dict[str, Any]]:
        """Logs the full response and treats both HTTP errors and a v3 `error` envelope as failure.
        v3 device endpoints return HTTP 200 with `{result: ...}` on success or `{error: {...}}` on
        rejection (wrong mode, device offline, bad args) — so a 200 alone does NOT mean success."""
        status = resp.status_code
        try:
            data = resp.json()
        except Exception:
            logger.debug(f"[handy] <- {method} {path} HTTP {status} [{elapsed_ms:.0f}ms] non-JSON body: {resp.text[:300]}")
            return None
        logger.debug(f"[handy] <- {method} {path} HTTP {status} [{elapsed_ms:.0f}ms] {str(data)[:500]}")
        if status >= 400:
            logger.warning(f"[handy] {method} {path} HTTP {status}: {str(data)[:300]}")
            return None
        if isinstance(data, dict) and data.get("error"):
            logger.warning(f"[handy] {method} {path} rejected by device (200+error): {data.get('error')}")
            return None
        return data

    async def _api_get(self, path: str) -> Optional[Dict[str, Any]]:
        url = f"{self._api_base()}/{path}"
        logger.debug(f"[handy] -> GET {url} (key ...{self.key[-4:] if self.key else '----'})")
        t0 = time.monotonic()
        try:
            resp = await _client().get(url, headers=self._headers())
            return self._parse("GET", path, resp, (time.monotonic() - t0) * 1000)
        except Exception as e:
            logger.warning(f"[handy] GET {path} network error: {e!r}")
            return None

    async def _api_put(self, path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = f"{self._api_base()}/{path}"
        logger.debug(f"[handy] -> PUT {url} body={body}")
        t0 = time.monotonic()
        try:
            resp = await _client().put(url, headers=self._headers(), json=body)
            return self._parse("PUT", path, resp, (time.monotonic() - t0) * 1000)
        except Exception as e:
            logger.warning(f"[handy] PUT {path} network error: {e!r}")
            return None


# --- multi-device session group -----------------------------------------

class HandySessionGroup:
    """Owns the per-`PlaySessionId` set of `HandyController`s (one per enabled device) and fans every
    lifecycle action out to them concurrently, with per-device isolation — one device failing never
    affects the others or video. Also owns the pre-activation abandonment watchdog for the session."""

    def __init__(self, session_id: str, scene: Dict[str, Any]):
        self.session_id = session_id
        self.scene = scene
        self.controllers: "list[HandyController]" = []
        self.lock = asyncio.Lock()
        self._built = False
        self._playback_started = False
        self._abandon_task: Optional[asyncio.Task] = None

    async def _ensure_built(self):
        """Seed the Stash-configured device (once) and instantiate one controller per enabled device.
        Idempotent."""
        if self._built:
            return
        self._built = True
        try:
            cfg = await stash_client.get_stash_interface_config()
            handy_devices.ensure_stash_seed(cfg.get("handyKey"))
        except Exception as e:
            logger.debug(f"[handy] stash seed skipped for session {self.session_id}: {e}")
        self.controllers = [
            HandyController(self.session_id, self.scene, device)
            for device in handy_devices.enabled_devices()
        ]
        if not self.controllers:
            logger.info(f"[handy] no enabled devices configured; nothing to drive for session {self.session_id}")

    async def _fan(self, method: str, *args):
        async def run(c: "HandyController"):
            try:
                await asyncio.wait_for(getattr(c, method)(*args), timeout=DEVICE_OP_TIMEOUT_S)
            except asyncio.TimeoutError:
                c.state = "failed"
                logger.warning(
                    f"[handy] {method} timed out (>{DEVICE_OP_TIMEOUT_S:.0f}s) for dev={c.label} "
                    f"session={self.session_id} — device marked failed; other devices unaffected"
                )
            except Exception as e:
                logger.debug(f"[handy] {method} error dev={c.label} session={self.session_id}: {e}")
        await asyncio.gather(*(run(c) for c in self.controllers), return_exceptions=True)

    async def preactivate(self):
        async with self.lock:
            await self._ensure_built()
            await self._fan("preactivate", False)  # group owns the abandonment watchdog
            if not self._playback_started and self.controllers:
                self._arm_abandon_timeout()

    async def handle_playing(self, position_seconds: float, is_paused: bool):
        async with self.lock:
            await self._ensure_built()
            self._cancel_abandon_timeout()
            if not self._playback_started:
                self._playback_started = True
                await self._fan("begin_playback", position_seconds, is_paused)
            else:
                await self._fan("on_progress", position_seconds, is_paused)

    async def teardown(self):
        async with self.lock:
            self._cancel_abandon_timeout()
            await self._fan("teardown")

    # abandonment watchdog (session-level: no /playing after PlaybackInfo → tear the group down)
    def _arm_abandon_timeout(self):
        self._abandon_task = asyncio.create_task(self._abandon_watchdog())

    def _cancel_abandon_timeout(self):
        if self._abandon_task and not self._abandon_task.done():
            self._abandon_task.cancel()
        self._abandon_task = None

    async def _abandon_watchdog(self):
        try:
            await asyncio.sleep(PREACTIVATION_ABANDON_S)
        except asyncio.CancelledError:
            return
        if self._playback_started:
            return
        logger.info(
            f"[handy] pre-activation abandoned (no playback in {PREACTIVATION_ABANDON_S}s) — "
            f"tearing down session={self.session_id}"
        )
        await self.teardown()
        async with _registry_lock:
            _groups.pop(self.session_id, None)
        _start_pos_by_session.pop(self.session_id, None)


# --- module fan-out API (called from userdata_routes) --------------------

async def _safe_handle_playing(session_id: str, scene: Dict[str, Any], position_seconds: float, is_paused: bool):
    try:
        async with _registry_lock:
            group = _groups.get(session_id)
            if group is None:
                group = HandySessionGroup(session_id, scene)
                _groups[session_id] = group
        await group.handle_playing(position_seconds, is_paused)
    except Exception as e:
        logger.error(f"[handy] unexpected error handling playing for session {session_id}: {e}")


async def _safe_handle_stopped(session_id: str):
    try:
        async with _registry_lock:
            group = _groups.pop(session_id, None)
        if group is not None:
            await group.teardown()
    except Exception as e:
        logger.error(f"[handy] unexpected error handling stopped for session {session_id}: {e}")


def notify_playing(session_id: str, scene: Optional[Dict[str, Any]], position_seconds: float, is_paused: bool):
    """Fire-and-forget: schedule Handy handling for a playing/progress event. Returns immediately
    so it can never delay the /sessions/playing 204 response. No-op unless the feature is enabled
    and the scene is interactive."""
    if not getattr(config, "ENABLE_HANDY_SYNC", False):
        return
    if not scene or not scene.get("interactive"):
        return
    try:
        asyncio.create_task(_safe_handle_playing(session_id, scene, position_seconds, is_paused))
    except Exception as e:
        logger.error(f"[handy] failed to schedule playing handler for session {session_id}: {e}")


def notify_stopped(session_id: str):
    """Fire-and-forget: schedule Handy teardown for a stopped event. No-op if no group exists
    for this session."""
    if session_id not in _groups:
        return
    try:
        asyncio.create_task(_safe_handle_stopped(session_id))
    except Exception as e:
        logger.error(f"[handy] failed to schedule stopped handler for session {session_id}: {e}")


async def _safe_preactivate(session_id: str, scene: Dict[str, Any]):
    try:
        async with _registry_lock:
            if session_id in _groups:
                return  # already (pre)activated for this session
            group = HandySessionGroup(session_id, scene)
            _groups[session_id] = group
        await group.preactivate()
    except Exception as e:
        logger.debug(f"[handy] preactivate error for session {session_id}: {e}")


def prewarm(scene: Optional[Dict[str, Any]]):
    """Fire-and-forget: when an interactive scene's PlaybackInfo is requested (about to play),
    pre-instantiate the controller — connect, estimate offset, set HSSP mode and load the script —
    so the first /playing event only has to issue the play command (moves ~1.7 s of setup off the
    critical path). No device motion happens until playback actually starts. No-op unless enabled and
    the scene is interactive. The session id mirrors PlaybackInfo's `stash_<scene_id>`."""
    if not getattr(config, "ENABLE_HANDY_SYNC", False):
        return
    if not scene or not scene.get("interactive"):
        return
    scene_id = scene.get("id")
    funscript_url = (scene.get("paths") or {}).get("funscript")
    if not scene_id or not funscript_url:
        return
    try:
        asyncio.create_task(_safe_preactivate(f"stash_{scene_id}", scene))
    except Exception as e:
        logger.debug(f"[handy] failed to schedule preactivate for scene {scene_id}: {e}")


def note_start_position(scene_id: str, start_seconds: float):
    """Record the exact start position (from the stream request's startTimeTicks) for a scene's
    session, so the first play uses it instead of guessing. 0 = play-from-beginning. No-op unless
    enabled."""
    if not getattr(config, "ENABLE_HANDY_SYNC", False):
        return
    if not scene_id:
        return
    try:
        _start_pos_by_session[f"stash_{scene_id}"] = float(start_seconds)
    except (TypeError, ValueError):
        pass


# --- device registry HTTP API (config GUI) -------------------------------

async def _probe_connected(device: Dict[str, Any]) -> bool:
    """Best-effort connection probe for one device, reusing the controller's v2/v3-aware
    `_get_connected`. Never raises."""
    try:
        c = HandyController("probe", {}, device)
        if not c.key:
            return False
        return await c._get_connected()
    except Exception:
        return False


async def endpoint_devices_list(request: Request) -> Response:
    """List configured Handy devices. Auto-seeds the Stash-configured connection key as a device on
    first sight (never deletes existing ones)."""
    try:
        cfg = await stash_client.get_stash_interface_config()
        handy_devices.ensure_stash_seed(cfg.get("handyKey"))
    except Exception as e:
        logger.debug(f"[handy] device-list stash seed skipped: {e}")
    return JSONResponse({"devices": handy_devices.list_devices(),
                         "application_id_set": bool(_app_id())})


async def endpoint_devices_create(request: Request) -> Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    device = handy_devices.add_device(body, source="manual")
    return JSONResponse(device, status_code=201)


async def endpoint_devices_update(request: Request) -> Response:
    device_id = request.path_params.get("device_id", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    device = handy_devices.update_device(device_id, body)
    if device is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(device)


async def endpoint_devices_delete(request: Request) -> Response:
    device_id = request.path_params.get("device_id", "")
    if not handy_devices.delete_device(device_id):
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"status": "deleted"})


async def endpoint_devices_status(request: Request) -> Response:
    """Probe every enabled device's Handy connection concurrently for the GUI status glow. Shares the
    handyfeeling rate budget, so the GUI polls this infrequently (tab open + every 30 s). No-op (no
    handyfeeling traffic) while the master toggle is off — the GUI leaves the dots neutral."""
    if not getattr(config, "ENABLE_HANDY_SYNC", False):
        return JSONResponse({"status": [], "disabled": True})
    devices = handy_devices.enabled_devices()
    results = await asyncio.gather(*(_probe_connected(d) for d in devices), return_exceptions=True)
    status = [
        {"id": d.get("id"), "connected": (r is True)}
        for d, r in zip(devices, results)
    ]
    return JSONResponse({"status": status})
