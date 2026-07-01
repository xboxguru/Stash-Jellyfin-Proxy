"""Feature 3 — Interactive Toy (Handy) Sync.

A per-PlaySessionId backend controller that drives a connected Handy device in sync with an
interactive scene's funscript, using the Jellyfin playback-reporting events the proxy already
receives (/sessions/playing[/progress], /sessions/playing/stopped in api/userdata_routes.py).

Handy REST API. Uses **v3** (handy-rest/v3) when `HANDY_APPLICATION_ID` is configured — sent as the
`X-Api-Key` header — otherwise falls back to **v2** (device connection key only, as Stash uses).
Lifecycle:
    connect probe -> estimate server-time offset -> mode(HSSP) -> hssp/setup(url)
    -> hssp/play(start_time, server_time) / hssp/stop

Protocol is chosen per session (see `_resolve_use_hsp`):
  - HSSP (cloud-hosted script URL) — the stable path (implemented here).
  - HSP  (local point-streaming)  — the beta path (Phase B; v3-only; currently falls back to HSSP).
The proxy setting `HANDY_SYNC_MODE` (auto|hosted|local) overrides Stash's `useStashHostedFunscript`.
See PLANNED_FEATURES §3.11 for the v2/v3 auth split.

ISOLATION CONTRACT (mandatory, see PLANNED_FEATURES §3.1):
The controller is a best-effort, fully isolated side-channel. Every public entry point is a
fire-and-forget scheduler and every Handy/network call is wrapped so an exception can NEVER reach
the /sessions/playing response or affect video playback. On any activation failure we log once,
mark the session's controller failed, and stop touching it — no retries.
"""

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

import config
from core import stash_client

logger = logging.getLogger(__name__)

# Handy REST API. v3 (handy-rest/v3) needs an ApplicationID (sent as X-Api-Key) on device
# endpoints; when `HANDY_APPLICATION_ID` is configured we use v3, otherwise we fall back to v2
# (which authenticates with only the device connection key — the same thing Stash uses). HSP
# (local streaming) is v3-only. See PLANNED_FEATURES §3.11.
HANDY_API_BASE_V2 = "https://www.handyfeeling.com/api/handy/v2"
HANDY_API_BASE_V3 = "https://www.handyfeeling.com/api/handy-rest/v3"
HANDY_UPLOAD_URL = "https://www.handyfeeling.com/api/sync/upload?local=true"

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

# Registry of live controllers, keyed by PlaySessionId.
_controllers: Dict[str, "HandyController"] = {}
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


def _resolve_use_hsp(cfg: Dict[str, Any]) -> bool:
    """Decide whether this session should use HSP (local streaming) vs HSSP (cloud hosting).

    Proxy setting `HANDY_SYNC_MODE` overrides Stash: `hosted` forces HSSP, `local` forces HSP,
    `auto` follows Stash's `useStashHostedFunscript` (local serving -> HSP)."""
    mode = str(getattr(config, "HANDY_SYNC_MODE", "auto")).strip().lower()
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


class HandyController:
    """Drives one Handy device for one PlaySessionId. All public methods are serialized by
    `self.lock`; state transitions: init -> ready | failed -> closed."""

    def __init__(self, session_id: str, scene: Dict[str, Any]):
        self.session_id = session_id
        self.scene_id = scene.get("id")
        self.stash_funscript_url = (scene.get("paths") or {}).get("funscript")
        try:
            self.resume_time_s = float(scene.get("resume_time") or 0)
        except (TypeError, ValueError):
            self.resume_time_s = 0.0
        self.lock = asyncio.Lock()
        self.state = "init"  # init | ready | failed | closed

        self.key: str = ""
        self.script_offset_ms: int = 0
        self.estimated_offset_ms: float = 0.0  # cs_offset
        self.app_id: str = _app_id()
        self.use_v3: bool = bool(self.app_id)

        self._is_playing = False
        self._playback_started = False   # True once the first play event has been handled
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
            self.key = (cfg.get("handyKey") or "").strip()
            try:
                self.script_offset_ms = int(cfg.get("funscriptOffset") or 0)
            except (TypeError, ValueError):
                self.script_offset_ms = 0
            use_hsp = _resolve_use_hsp(cfg)
            logger.debug(
                f"[handy] preparing session={self.session_id} scene={self.scene_id} "
                f"api={'v3' if self.use_v3 else 'v2'} key=...{(self.key[-4:] if self.key else '----')} "
                f"app_id={'set' if self.app_id else 'none'} script_offset={self.script_offset_ms}ms "
                f"sync_mode={getattr(config, 'HANDY_SYNC_MODE', 'auto')} use_hsp={use_hsp}"
            )

            if not self.key:
                logger.info(f"[handy] no handyKey configured in Stash; disabling sync for session {self.session_id}")
                self.state = "failed"
                return

            # Phase A: HSP (local streaming) is not implemented yet — fall back to HSSP so the
            # 'local' selection still plays (via cloud hosting) instead of failing.
            if use_hsp:
                logger.info(
                    f"[handy] HSP (local streaming) selected for session {self.session_id} but not yet "
                    f"implemented (Phase B) — using HSSP (hosted) for now"
                )

            if not await self._get_connected():
                logger.info(f"[handy] device not connected (key ...{self.key[-4:]}); disabling sync for session {self.session_id}")
                self.state = "failed"
                return

            self.estimated_offset_ms = await self._estimate_offset()
            if self.estimated_offset_ms == 0.0:
                logger.warning(
                    f"[handy] server-time offset estimated as 0 for session {self.session_id} — "
                    f"all /servertime samples failed to parse; sync timing will be unreliable"
                )

            # HSSP requires a publicly-hosted script URL (private URLs rejected on FW 4.2.x).
            script_url = await _prepare_upload_url(self.scene_id, self.stash_funscript_url)
            if not script_url:
                logger.info(f"[handy] funscript unavailable for scene {self.scene_id}; disabling sync for session {self.session_id}")
                self.state = "failed"
                return

            if not await self._set_mode(MODE_HSSP):
                logger.info(f"[handy] could not set HSSP mode for session {self.session_id}; disabling sync")
                self.state = "failed"
                return
            if not await self._hssp_setup(script_url):
                logger.info(f"[handy] HSSP setup failed for scene {self.scene_id}; disabling sync for session {self.session_id}")
                self.state = "failed"
                return

            # Give the device a moment to download/prepare the script before the first play.
            await asyncio.sleep(SETUP_SETTLE_S)

            self.state = "ready"
            logger.info(
                f"[handy] prepared (HSSP {'v3' if self.use_v3 else 'v2'}) session={self.session_id} "
                f"scene={self.scene_id} cs_offset={self.estimated_offset_ms:.0f}ms "
                f"script_offset={self.script_offset_ms}ms"
            )
        except Exception as e:
            logger.warning(f"[handy] prepare failed for session {self.session_id}: {e}")
            self.state = "failed"

    async def preactivate(self):
        """Pre-instantiate on PlaybackInfo: run _prepare() so the device is set up and the clock
        synced, but do NOT play (no motion until the user actually starts). Arms an abandonment
        timeout so a browse-but-don't-play leaves nothing lingering."""
        async with self.lock:
            if self.state != "init":
                return
            await self._prepare()
            if not self._playback_started:
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
                f"[handy] begin playback session={self.session_id} @ {initial_pos:.1f}s "
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
            f"tearing down session={self.session_id}"
        )
        await self.teardown()
        async with _registry_lock:
            _controllers.pop(self.session_id, None)
        _start_pos_by_session.pop(self.session_id, None)

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

                # Playing: infer a seek when reported position diverges from wall-clock. Debounced so
                # a scrub (many rapid pings) collapses into one play at the settled position.
                if prev_pos is not None and prev_t is not None:
                    d_pos = position_seconds - prev_pos
                    d_wall = now - prev_t
                    if abs(d_pos - d_wall) > SEEK_THRESHOLD_S:
                        logger.info(
                            f"[handy] seek detected {prev_pos:.1f}->{position_seconds:.1f}s "
                            f"(Δpos={d_pos:.1f} Δwall={d_wall:.1f}) -> re-play (debounced), session={self.session_id}"
                        )
                        self._schedule_play(position_seconds)
            except Exception as e:
                logger.debug(f"[handy] progress handling error for session {self.session_id}: {e}")

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
                if self.state == "ready" and self._is_playing:
                    await self._stop()
            except Exception as e:
                logger.debug(f"[handy] teardown stop error for session {self.session_id}: {e}")
            finally:
                self.state = "closed"
                _start_pos_by_session.pop(self.session_id, None)
                logger.info(f"[handy] torn down session={self.session_id} scene={self.scene_id}")

    # --- Handy command primitives (v3) ----------------------------------

    async def _play(self, position_seconds: float):
        # server_time = estimated offset + now (Tcest). v3 uses snake_case keys, v2 camelCase.
        start_time = round(position_seconds * 1000 + self.script_offset_ms)
        server_time = round(self.estimated_offset_ms + _now_ms())
        if self.use_v3:
            body = {"start_time": start_time, "server_time": server_time}
        else:
            body = {"startTime": start_time, "serverTime": server_time}
        result = await self._api_put("hssp/play", body)
        if result is None:
            logger.warning(f"[handy] play@{start_time}ms NOT accepted by device, session={self.session_id}")
            return
        self._is_playing = True
        logger.info(f"[handy] play@{start_time}ms (server_time={server_time}) session={self.session_id}")

    async def _stop(self):
        await self._api_put("hssp/stop", {})
        self._is_playing = False
        logger.info(f"[handy] stop session={self.session_id}")

    async def _set_mode(self, mode_value: int) -> bool:
        # v3 uses /mode2 (needs the ApplicationID); v2 uses /mode.
        endpoint = "mode2" if self.use_v3 else "mode"
        result = await self._api_put(endpoint, {"mode": mode_value})
        return result is not None

    async def _hssp_setup(self, script_url: str) -> bool:
        result = await self._api_put("hssp/setup", {"url": script_url})
        return result is not None

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


# --- module fan-out API (called from userdata_routes) --------------------

async def _safe_handle_playing(session_id: str, scene: Dict[str, Any], position_seconds: float, is_paused: bool):
    try:
        async with _registry_lock:
            controller = _controllers.get(session_id)
            if controller is None:
                controller = HandyController(session_id, scene)
                _controllers[session_id] = controller
        # First real /playing → begin playback (prepares inline if not pre-activated); thereafter
        # events are progress updates.
        if not controller._playback_started:
            await controller.begin_playback(position_seconds, is_paused)
        else:
            await controller.on_progress(position_seconds, is_paused)
    except Exception as e:
        logger.error(f"[handy] unexpected error handling playing for session {session_id}: {e}")


async def _safe_handle_stopped(session_id: str):
    try:
        async with _registry_lock:
            controller = _controllers.pop(session_id, None)
        if controller is not None:
            await controller.teardown()
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
    """Fire-and-forget: schedule Handy teardown for a stopped event. No-op if no controller exists
    for this session."""
    if session_id not in _controllers:
        return
    try:
        asyncio.create_task(_safe_handle_stopped(session_id))
    except Exception as e:
        logger.error(f"[handy] failed to schedule stopped handler for session {session_id}: {e}")


async def _safe_preactivate(session_id: str, scene: Dict[str, Any]):
    try:
        async with _registry_lock:
            if session_id in _controllers:
                return  # already (pre)activated for this session
            controller = HandyController(session_id, scene)
            _controllers[session_id] = controller
        await controller.preactivate()
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


# --- LAN funscript serving (retained for a future publicly-reachable deployment) -----------

async def endpoint_funscript(request: Request) -> Response:
    """Serves a scene's funscript for a Handy to fetch directly. NOTE: on FW 4.2.x, HSSP no longer
    accepts private-network URLs, so this is not used by the current HSSP path; it is retained for a
    future publicly-reachable deployment. Re-fetches from Stash with our API key.

    Default response is raw .funscript JSON; `?format=csv` serves Handy CSV."""
    if not getattr(config, "ENABLE_HANDY_SYNC", False):
        return PlainTextResponse("Handy sync disabled", status_code=404)
    scene_id = request.path_params.get("scene_id", "")
    fmt = request.query_params.get("format", "json").lower()
    funscript_url = f"{config.get_stash_base()}/scene/{scene_id}/funscript"
    funscript = await _fetch_funscript(funscript_url)
    if not funscript:
        return PlainTextResponse("funscript unavailable", status_code=404)
    if fmt == "csv":
        return PlainTextResponse(funscript_to_csv(funscript), media_type="text/csv")
    return JSONResponse(funscript)
