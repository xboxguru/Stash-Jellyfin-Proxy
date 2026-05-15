import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

import config
from core.jellyfin_mapper import encode_id

logger = logging.getLogger(__name__)

_live_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
CACHE_TTL = 300           # 5-min TTL for M3U/XMLTV/channel-list caches
_SCHEDULE_TTL = 86400.0   # rebuild Stash schedules every 24 h

_m3u_cache: dict = {"data": None, "ts": 0.0}
_xmltv_cache: dict = {"data": None, "ts": 0.0}

_channel_stream_map: dict[str, str] = {}   # encoded_id -> stream_url
_channel_info_map: dict[str, dict] = {}    # encoded_id -> raw channel dict
_program_info_map: dict[str, dict] = {}    # encoded_id -> raw program dict

# Stash dynamic-channel state
_stash_channels_cache: dict = {"data": None, "ts": 0.0}
_stash_channel_map: dict[str, dict] = {}   # encoded_id -> stash channel dict
_stash_schedule: dict[str, list] = {}      # tvg_id -> sorted list of schedule entries
_stash_schedule_built_at: float = 0.0
_rebuild_lock: asyncio.Lock = asyncio.Lock()

class _FFmpegChannelManager:
    """One FFmpeg HLS process per active channel, started on first play request.

    FFmpeg reads a ffconcat playlist of raw Stash scene URLs (no Stash-side
    transcode) and writes HLS segments to a per-channel temp directory.
    An idle watchdog shuts down the process and deletes the temp dir after
    LIVE_TV_IDLE_TIMEOUT seconds of no manifest/segment requests.
    """

    def __init__(self):
        self._procs:  dict[str, asyncio.subprocess.Process] = {}
        self._dirs:   dict[str, str]        = {}
        self._last:   dict[str, float]      = {}   # channel_id → last request timestamp
        self._stderr: dict[str, list[str]]  = {}   # channel_id → rolling stderr lines
        self._lock    = asyncio.Lock()
        self._watchdog: asyncio.Task | None = None

    # ── public API ─────────────────────────────────────────────────────────

    def touch(self, cid: str) -> None:
        """Record activity; restarts the idle watchdog if needed."""
        self._last[cid] = time.time()
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._idle_loop())

    def manifest_path(self, cid: str) -> str | None:
        d = self._dirs.get(cid)
        if not d:
            return None
        p = os.path.join(d, "stream.m3u8")
        return p if os.path.exists(p) else None

    def seg_dir(self, cid: str) -> str | None:
        return self._dirs.get(cid)

    def is_alive(self, cid: str) -> bool:
        p = self._procs.get(cid)
        return p is not None and p.returncode is None

    async def ensure(self, cid: str, entries: list[dict], seek: float) -> bool:
        """Start FFmpeg for the channel if it isn't already running."""
        async with self._lock:
            if self.is_alive(cid) and self.manifest_path(cid):
                return True
            await self._stop_locked(cid)
            return await self._launch(cid, entries, seek)

    async def stop(self, cid: str) -> None:
        async with self._lock:
            await self._stop_locked(cid)

    async def cleanup_all(self) -> None:
        for cid in list(self._procs.keys()):
            await self.stop(cid)

    # ── internals ──────────────────────────────────────────────────────────

    async def _launch(self, cid: str, entries: list[dict], seek: float) -> bool:
        d = tempfile.mkdtemp(prefix=f"sjp_{cid[:8]}_")
        self._dirs[cid] = d

        stash_base = config.get_stash_base()
        api_key    = getattr(config, "STASH_API_KEY", "")

        # Build ffconcat playlist — raw Stash stream URLs, no Stash transcode.
        # inpoint on the first entry tells FFmpeg to seek before outputting,
        # handled via HTTP byte-range so Stash never re-encodes.
        concat_path = os.path.join(d, "concat.txt")
        with open(concat_path, "w", encoding="utf-8") as f:
            f.write("ffconcat version 1.0\n")
            for i, entry in enumerate(entries):
                url = f"{stash_base}/scene/{entry['scene_id']}/stream"
                if api_key:
                    url += f"?apikey={api_key}"
                f.write(f"file '{url}'\n")
                if i == 0 and seek > 1.0:
                    f.write(f"inpoint {seek:.3f}\n")

        ffmpeg_bin = getattr(config, "FFMPEG_PATH", "ffmpeg")
        # FFmpeg on Windows: use forward slashes to avoid backslash escaping issues
        seg_tmpl = os.path.join(d, "seg%05d.ts").replace("\\", "/")
        manifest = os.path.join(d, "stream.m3u8").replace("\\", "/")
        concat_fwd = concat_path.replace("\\", "/")

        cmd = [
            ffmpeg_bin, "-y",
            # Allow http/https in the ffconcat file entries.
            # Without this FFmpeg rejects non-file:// URLs (exits rc=-22).
            "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
            "-f", "concat", "-safe", "0",
            "-i", concat_fwd,
            # Single transcode to consistent H.264+AAC — works regardless of
            # source codec/container.  veryfast keeps CPU usage low.
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-force_key_frames", "expr:gte(t,n_forced*4)",
            "-c:a", "aac", "-b:a", "192k",
            "-hls_time", "4",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments+append_list+omit_endlist",
            "-hls_segment_filename", seg_tmpl,
            manifest,
        ]

        logger.info(
            f"LiveTV FFmpeg: launching channel {cid!r} — "
            f"{len(entries)} scenes, seek={seek:.1f}s"
        )
        logger.debug(f"LiveTV FFmpeg cmd: {' '.join(cmd)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception as exc:
            logger.error(f"LiveTV FFmpeg: launch failed — {exc}")
            shutil.rmtree(d, ignore_errors=True)
            self._dirs.pop(cid, None)
            return False

        self._procs[cid] = proc
        self._stderr[cid] = []
        # Continuously drain stderr so the pipe buffer never fills and
        # blocks FFmpeg.  The last 60 lines are kept for error reporting.
        asyncio.create_task(self._drain_stderr(proc, cid))

        # Wait up to 30 s for the first manifest file to appear.
        for _ in range(60):
            if os.path.exists(manifest):
                logger.info(f"LiveTV FFmpeg: channel {cid!r} ready")
                return True
            if proc.returncode is not None:
                logger.error(
                    f"LiveTV FFmpeg: exited prematurely (rc={proc.returncode})"
                )
                buf = self._stderr.get(cid, [])
                error_lines = [l for l in buf if not l.startswith(("ffmpeg version", "  built", "  config", "  lib"))]
                if error_lines:
                    logger.error("LiveTV FFmpeg stderr (errors):\n" + "\n".join(error_lines[-30:]))
                return False
            await asyncio.sleep(0.5)

        logger.error("LiveTV FFmpeg: timed out waiting for first segment")
        return False

    async def _drain_stderr(self, proc: asyncio.subprocess.Process, cid: str) -> None:
        """Read FFmpeg stderr continuously to prevent the pipe buffer from filling."""
        buf = self._stderr.setdefault(cid, [])
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                buf.append(line.decode(errors="replace").rstrip())
                if len(buf) > 60:
                    buf.pop(0)
        except Exception:
            pass

    async def _stop_locked(self, cid: str) -> None:
        proc = self._procs.pop(cid, None)
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
        d = self._dirs.pop(cid, None)
        if d:
            shutil.rmtree(d, ignore_errors=True)
        self._stderr.pop(cid, None)

    async def _idle_loop(self) -> None:
        idle_secs = float(getattr(config, "LIVE_TV_IDLE_TIMEOUT", 60))
        while self._procs:
            await asyncio.sleep(20)
            now  = time.time()
            idle = [
                cid for cid, ts in list(self._last.items())
                if now - ts > idle_secs
            ]
            for cid in idle:
                logger.info(
                    f"LiveTV FFmpeg: channel {cid!r} idle "
                    f"{idle_secs:.0f}s — shutting down"
                )
                await self.stop(cid)
                self._last.pop(cid, None)


_ffmpeg_manager = _FFmpegChannelManager()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_uuid_key(hex_id: str) -> str:
    """Convert a 32-char hex ID to hyphenated UUID key format (8-4-4-4-12)."""
    h = hex_id.replace("-", "")[:32].ljust(32, "0")
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _logo_tag(logo_url: str) -> str:
    """Stable image-tag hash derived from the logo URL."""
    return hashlib.md5(logo_url.encode()).hexdigest() if logo_url else ""


def _stash_screenshot_url(scene_id: str) -> str:
    """Return the proxied Stash screenshot URL for a scene."""
    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    url = f"{stash_base}/scene/{scene_id}/screenshot"
    return f"{url}?apikey={apikey}" if apikey else url


def _live_tv_enabled() -> bool:
    """True if any Live TV source is enabled and the master switch is on."""
    if not getattr(config, "ENABLE_LIVE_TV", False):
        return False
    return getattr(config, "ENABLE_TUNARR", False) or getattr(config, "ENABLE_STASH_CHANNELS", False)


def _schedule_path() -> str:
    log_dir = getattr(config, "LOG_DIR", "/config")
    return os.path.join(log_dir, "stash_schedule.json")


def _save_schedule():
    try:
        path = _schedule_path()
        tmp = path + ".tmp"
        payload = {"built_at": _stash_schedule_built_at, "schedule": _stash_schedule}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
        logger.info(f"LiveTV: schedule saved to {path}")
    except Exception as e:
        logger.warning(f"LiveTV: could not save schedule: {e}")


def _load_schedule():
    global _stash_schedule, _stash_schedule_built_at
    path = _schedule_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        _stash_schedule_built_at = float(payload.get("built_at", 0))
        _stash_schedule = payload.get("schedule", {})
        age_h = (time.time() - _stash_schedule_built_at) / 3600
        logger.info(f"LiveTV: loaded schedule from disk ({len(_stash_schedule)} channels, {age_h:.1f}h old)")
    except Exception as e:
        logger.warning(f"LiveTV: could not load schedule: {e}")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def _parse_m3u(content: str) -> list[dict]:
    channels = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF:"):
            attrs: dict[str, str] = {}
            for m in re.finditer(r'([\w-]+)="([^"]*)"', line):
                attrs[m.group(1)] = m.group(2)
            display_name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            stream_url = lines[i].strip() if i < len(lines) and not lines[i].startswith("#") else ""
            tvg_id = attrs.get("tvg-id") or display_name or str(len(channels))
            channels.append({
                "tvg_id": tvg_id,
                "name": attrs.get("tvg-name") or display_name,
                "logo": attrs.get("tvg-logo", ""),
                "number": attrs.get("tvg-chno", str(len(channels) + 1)),
                "stream_url": stream_url,
            })
        i += 1
    return channels


def _parse_xmltv_dt(s: str) -> tuple[str, float]:
    """Return (ISO-8601 UTC string, unix timestamp). Both empty/0 on failure."""
    try:
        parts = s.strip().split()
        dt = datetime.strptime(parts[0], "%Y%m%d%H%M%S")
        if len(parts) > 1:
            sign = 1 if parts[1][0] == "+" else -1
            dt -= timedelta(hours=int(parts[1][1:3]), minutes=int(parts[1][3:5])) * sign
        ts = dt.replace(tzinfo=timezone.utc).timestamp()
        return dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"), ts
    except Exception:
        return "", 0.0


def _parse_xmltv(content: str) -> list[dict]:
    programs = []
    try:
        root = ET.fromstring(content)
        for prog in root.findall("programme"):
            title_el = prog.find("title")
            desc_el = prog.find("desc")
            cat_el = prog.find("category")
            date_el = prog.find("date")
            start_iso, start_ts = _parse_xmltv_dt(prog.get("start", ""))
            stop_iso, stop_ts = _parse_xmltv_dt(prog.get("stop", ""))
            duration_ticks = max(0, int((stop_ts - start_ts) * 10_000_000)) if stop_ts and start_ts else 0
            year = None
            if date_el is not None and date_el.text:
                try:
                    year = int(date_el.text[:4])
                except ValueError:
                    pass
            icon_el = prog.find("icon")
            icon_url = icon_el.get("src", "") if icon_el is not None else ""
            programs.append({
                "channel_id": prog.get("channel", ""),
                "title": title_el.text if title_el is not None else "Unknown",
                "desc": desc_el.text if desc_el is not None else "",
                "genre": cat_el.text if cat_el is not None else "",
                "year": year,
                "start": start_iso,
                "start_ts": start_ts,
                "stop": stop_iso,
                "stop_ts": stop_ts,
                "run_time_ticks": duration_ticks,
                "icon": icon_url,
            })
    except Exception as e:
        logger.warning(f"XMLTV parse error: {e}")
    return programs


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------

async def _get_channels() -> list[dict]:
    now = time.time()
    if _m3u_cache["data"] is not None and now - _m3u_cache["ts"] < CACHE_TTL:
        return _m3u_cache["data"]

    m3u_url = getattr(config, "TUNER_M3U_URL", "")
    if not m3u_url:
        return []

    try:
        resp = await _live_client.get(m3u_url)
        resp.raise_for_status()
        channels = _parse_m3u(resp.text)
        _m3u_cache["data"] = channels
        _m3u_cache["ts"] = now
        _channel_stream_map.clear()
        # Remove stale Tunarr entries without disturbing Stash channel entries
        for k in [k for k, v in _channel_info_map.items() if not v.get("stash_type")]:
            _channel_info_map.pop(k, None)
        for ch in channels:
            eid = encode_id("channel", ch["tvg_id"])
            _channel_stream_map[eid] = ch["stream_url"]
            _channel_info_map[eid] = ch
        logger.info(f"LiveTV: loaded {len(channels)} channels from M3U")
        return channels
    except Exception as e:
        logger.warning(f"LiveTV: failed to fetch M3U: {e}")
        return _m3u_cache["data"] or []


async def _get_programs() -> list[dict]:
    now = time.time()
    if _xmltv_cache["data"] is not None and now - _xmltv_cache["ts"] < CACHE_TTL:
        return _xmltv_cache["data"]

    xmltv_url = getattr(config, "TUNER_XMLTV_URL", "")
    if not xmltv_url:
        return []

    try:
        resp = await _live_client.get(xmltv_url)
        resp.raise_for_status()
        programs = _parse_xmltv(resp.text)
        _xmltv_cache["data"] = programs
        _xmltv_cache["ts"] = now
        _program_info_map.clear()
        for prog in programs:
            eid = encode_id("program", f"{prog['channel_id']}|{prog['start']}")
            _program_info_map[eid] = prog
        logger.info(f"LiveTV: loaded {len(programs)} programs from XMLTV")
        return programs
    except Exception as e:
        logger.warning(f"LiveTV: failed to fetch XMLTV: {e}")
        return _xmltv_cache["data"] or []


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Dynamic Stash Channels
# ---------------------------------------------------------------------------

async def _fetch_scenes_for_stash_channel(ch: dict) -> list[dict]:
    """Return [{id, title, duration_sec}] for the given Stash channel config."""
    from core.stash_client import call_graphql
    channel_type = ch.get("stash_type", "")

    if channel_type == "tag":
        scene_filter = {"tags": {"value": [ch["stash_id"]], "modifier": "INCLUDES", "depth": 1}}
        query = """
        query($sf: SceneFilterType) {
            findScenes(filter: {per_page: -1, sort: "id", direction: ASC}, scene_filter: $sf) {
                scenes { id title files { duration } }
            }
        }
        """
        data = await call_graphql(query, {"sf": scene_filter})
    elif channel_type == "filter":
        from core.query_builder import transform_saved_filter
        scene_filter: dict = {}
        filter_args: dict = {"per_page": -1, "sort": "id", "direction": "ASC"}
        object_filter = ch.get("object_filter")
        if object_filter:
            scene_filter = transform_saved_filter(object_filter)
        else:
            # Legacy format: re-fetch to get the raw filter JSON string
            from core.stash_client import get_saved_filters
            saved = await get_saved_filters()
            fd = next((f for f in saved if str(f.get("id")) == str(ch["stash_id"])), None)
            if fd:
                if fd.get("object_filter"):
                    scene_filter = transform_saved_filter(fd["object_filter"])
                elif fd.get("filter"):
                    import json as _json
                    parsed = _json.loads(fd["filter"])
                    if "scene_filter" in parsed:
                        scene_filter = transform_saved_filter(parsed["scene_filter"])
                    for k in ("q", "sort", "direction"):
                        if k in parsed:
                            filter_args[k] = parsed[k]
        query = """
        query($filter: FindFilterType, $sf: SceneFilterType) {
            findScenes(filter: $filter, scene_filter: $sf) {
                scenes { id title files { duration } }
            }
        }
        """
        data = await call_graphql(query, {"filter": filter_args, "sf": scene_filter})
    elif channel_type == "shorts":
        max_secs = int(getattr(config, "SHORTS_MAX_MINUTES", 5)) * 60
        scene_filter = {"duration": {"value": max_secs, "modifier": "LESS_THAN"}}
        query = """
        query($sf: SceneFilterType) {
            findScenes(filter: {per_page: -1, sort: "id", direction: ASC}, scene_filter: $sf) {
                scenes { id title files { duration } }
            }
        }
        """
        data = await call_graphql(query, {"sf": scene_filter})
    else:
        data = None
    raw = (data or {}).get("findScenes", {}).get("scenes", [])

    result = []
    for s in raw:
        files = s.get("files") or []
        duration = float(files[0].get("duration") or 0) if files else 0.0
        if duration >= 5.0:
            result.append({"id": s["id"], "title": s.get("title") or f"Scene {s['id']}", "duration_sec": duration})
    return result


def _build_random_schedule(scenes: list[dict]) -> list[dict]:
    """Build a time-indexed schedule by drawing scenes in random order.

    Fills the window [now - KEEP_DAYS, now + SCHEDULE_DAYS].  Scenes are
    drawn from a shuffled pool; when the pool is exhausted it is refilled and
    reshuffled so no scene repeats until every other scene has aired.
    """
    if not scenes:
        return []

    keep_days = max(1, int(getattr(config, "STASH_KEEP_DAYS", 2)))
    sched_days = max(1, int(getattr(config, "STASH_SCHEDULE_DAYS", 7)))

    now = time.time()
    window_start = now - keep_days * 86400
    window_end = now + sched_days * 86400

    pool: list[dict] = []
    entries: list[dict] = []
    cursor = window_start

    while cursor < window_end:
        if not pool:
            pool = list(scenes)
            random.shuffle(pool)
        s = pool.pop()
        stop = cursor + s["duration_sec"]
        entries.append({
            "start_ts": cursor,
            "stop_ts": stop,
            "scene_id": s["id"],
            "title": s["title"],
            "duration_sec": s["duration_sec"],
        })
        cursor = stop

    return entries


def _build_shorts_block_schedule() -> list[dict]:
    """Build synthetic 30-minute EPG blocks for the Shorts channel.

    Individual scenes are too short (< 5 min) to render as visible cells in
    most TV guide UIs.  Instead we emit 30-minute blocks labelled "Shorts" so
    the guide looks normal.  The actual scene playlist is assembled on-demand
    when a user plays the channel (see endpoint_stash_channel_stream).
    """
    BLOCK_SECS = 1800  # 30 minutes per guide cell

    keep_days = max(1, int(getattr(config, "STASH_KEEP_DAYS", 2)))
    sched_days = max(1, int(getattr(config, "STASH_SCHEDULE_DAYS", 7)))

    now = time.time()
    window_start = now - keep_days * 86400
    window_end   = now + sched_days * 86400

    entries: list[dict] = []
    cursor = window_start
    while cursor < window_end:
        entries.append({
            "start_ts":    cursor,
            "stop_ts":     cursor + BLOCK_SECS,
            "scene_id":    None,          # synthetic — no individual scene
            "title":       "Shorts",
            "duration_sec": BLOCK_SECS,
        })
        cursor += BLOCK_SECS

    return entries


async def _get_stash_channels() -> list[dict]:
    """Query Stash and return the list of dynamically configured channels."""
    from core import stash_client
    now = time.time()
    if _stash_channels_cache["data"] is not None and now - _stash_channels_cache["ts"] < CACHE_TTL:
        return _stash_channels_cache["data"]

    channels: list[dict] = []
    start_num = int(getattr(config, "STASH_CHANNEL_START_NUMBER", 5001))

    def _next_num():
        nonlocal start_num
        n = start_num
        start_num += 1
        return str(n)

    # Tag-based channels
    raw_tags = getattr(config, "STASH_TV_TAGS", "") or ""
    tag_names = [t.strip() for t in (raw_tags if isinstance(raw_tags, list) else raw_tags.split(",")) if str(t).strip()]
    if tag_names:
        all_tags = await stash_client.get_all_tags()
        tags_by_name = {t["name"].lower(): t for t in all_tags}
        for name in tag_names:
            tag = tags_by_name.get(name.lower())
            if not tag:
                logger.warning(f"LiveTV: tag '{name}' not found in Stash — skipping channel")
                continue
            tvg_id = f"t{tag['id']}"
            logo = tag.get("image_path") or ""
            if logo:
                if not logo.startswith("http"):
                    logo = f"{config.get_stash_base()}{logo}"
                api_key = getattr(config, "STASH_API_KEY", "")
                if api_key and "apikey=" not in logo:
                    logo += f"{'&' if '?' in logo else '?'}apikey={api_key}"
            channels.append({"tvg_id": tvg_id, "name": name, "number": _next_num(),
                              "logo": logo, "stash_type": "tag", "stash_id": tag["id"]})

    # Saved-filter channels
    raw_filters = getattr(config, "STASH_TV_FILTERS", "") or ""
    filter_names = [f.strip() for f in (raw_filters if isinstance(raw_filters, list) else raw_filters.split(",")) if str(f).strip()]
    if filter_names:
        saved = await stash_client.get_saved_filters()
        filters_by_name = {f["name"].lower(): f for f in saved}
        for name in filter_names:
            sf = filters_by_name.get(name.lower())
            if not sf:
                logger.warning(f"LiveTV: saved filter '{name}' not found in Stash — skipping channel")
                continue
            tvg_id = f"f{sf['id']}"
            channels.append({"tvg_id": tvg_id, "name": name, "number": _next_num(),
                              "logo": "", "stash_type": "filter", "stash_id": sf["id"],
                              "object_filter": sf.get("object_filter"),
                              "find_filter": sf.get("find_filter")})

    # Shorts channel
    if getattr(config, "ENABLE_SHORTS_CHANNEL", False):
        channels.append({"tvg_id": "shorts", "name": "Shorts", "number": _next_num(),
                         "logo": "", "stash_type": "shorts"})

    _stash_channels_cache["data"] = channels
    _stash_channels_cache["ts"] = now

    # Register in the shared lookup map
    for ch in channels:
        enc = encode_id("channel", ch["tvg_id"])
        _channel_info_map[enc] = ch
        _channel_info_map[enc.replace("-", "")] = ch
        _stash_channel_map[enc] = ch
        _stash_channel_map[enc.replace("-", "")] = ch

    logger.info(f"LiveTV: {len(channels)} Stash channels configured")
    return channels


async def _rebuild_stash_schedules():
    """Fetch scenes for every Stash channel and rebuild all schedules."""
    global _stash_schedule, _stash_schedule_built_at

    if not getattr(config, "ENABLE_STASH_CHANNELS", False):
        return

    async with _rebuild_lock:
        channels = await _get_stash_channels()
        new_schedule: dict[str, list] = {}

        for ch in channels:
            tvg_id = ch["tvg_id"]
            try:
                if ch.get("stash_type") == "shorts":
                    # Verify scenes exist, but build synthetic 30-min EPG blocks
                    # (individual clips are too short to render in guide UIs).
                    scenes = await _fetch_scenes_for_stash_channel(ch)
                    if not scenes:
                        logger.warning(f"LiveTV: no scenes for channel '{ch['name']}' — EPG will be empty")
                        continue
                    slots = _build_shorts_block_schedule()
                    new_schedule[tvg_id] = slots
                    logger.info(f"LiveTV: schedule built for '{ch['name']}' — {len(scenes)} scenes, {len(slots)} 30-min EPG blocks")
                else:
                    scenes = await _fetch_scenes_for_stash_channel(ch)
                    if not scenes:
                        logger.warning(f"LiveTV: no scenes for channel '{ch['name']}' — EPG will be empty")
                        continue
                    slots = _build_random_schedule(scenes)
                    new_schedule[tvg_id] = slots
                    logger.info(f"LiveTV: schedule built for '{ch['name']}' — {len(scenes)} scenes, {len(slots)} EPG slots")
            except Exception as e:
                logger.error(f"LiveTV: schedule build failed for '{ch['name']}': {e}", exc_info=True)

        _stash_schedule = new_schedule
        _stash_schedule_built_at = time.time()
        _save_schedule()


async def _ensure_stash_schedules():
    """Rebuild schedules if stale or empty (safety net for first request)."""
    if time.time() - _stash_schedule_built_at > _SCHEDULE_TTL:
        await _rebuild_stash_schedules()


# ---------------------------------------------------------------------------
# Background maintenance task
# ---------------------------------------------------------------------------

_maintenance_task: asyncio.Task | None = None


async def _schedule_maintenance_loop():
    """Prune old schedule entries and extend the window — runs every 24 hours."""
    while True:
        await asyncio.sleep(_SCHEDULE_TTL)
        logger.info("LiveTV: 24h maintenance — pruning old entries and extending schedule window")
        try:
            await _rebuild_stash_schedules()
        except Exception as e:
            logger.error(f"LiveTV: scheduled maintenance failed: {e}", exc_info=True)


async def start_maintenance_task():
    """Start the background schedule-maintenance loop (called from app lifespan)."""
    global _maintenance_task
    if _maintenance_task and not _maintenance_task.done():
        return
    _maintenance_task = asyncio.create_task(_schedule_maintenance_loop())
    logger.info("LiveTV: schedule maintenance task started (interval: 24h)")


async def stop_maintenance_task():
    """Cancel the maintenance loop (called from app lifespan on shutdown)."""
    global _maintenance_task
    if _maintenance_task and not _maintenance_task.done():
        _maintenance_task.cancel()
        try:
            await _maintenance_task
        except asyncio.CancelledError:
            pass
    _maintenance_task = None


def _get_stash_programs_for_channel(ch: dict, server_id: str, channels_by_tvg_id: dict) -> list[dict]:
    """Return Jellyfin-formatted programs from the Stash schedule for one channel."""
    tvg_id = ch["tvg_id"]
    schedule = _stash_schedule.get(tvg_id, [])
    now = time.time()
    keep_days = int(getattr(config, "STASH_KEEP_DAYS", 2))
    sched_days = int(getattr(config, "STASH_SCHEDULE_DAYS", 7))
    window_start = now - keep_days * 86400
    window_end = now + sched_days * 86400

    progs = []
    for entry in schedule:
        if entry["stop_ts"] < window_start or entry["start_ts"] > window_end:
            continue
        start_dt = datetime.fromtimestamp(entry["start_ts"], timezone.utc)
        stop_dt = datetime.fromtimestamp(entry["stop_ts"], timezone.utc)
        raw_prog = {
            "channel_id": tvg_id,
            "title": entry["title"],
            "start": start_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
            "stop": stop_dt.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
            "start_ts": entry["start_ts"],
            "stop_ts": entry["stop_ts"],
            "run_time_ticks": int(entry["duration_sec"] * 10_000_000),
            "genre": "",
            "desc": "",
            "scene_id": entry["scene_id"],
            "icon": _stash_screenshot_url(entry["scene_id"]) if entry.get("scene_id") else "",
        }
        prog_id = encode_id("program", f"{tvg_id}|{raw_prog['start']}")
        jellyfin_prog = _program_to_jellyfin(raw_prog, server_id, channels_by_tvg_id, prog_id)
        # Register for single-item lookup
        enc = prog_id.replace("-", "")
        _program_info_map[enc] = raw_prog
        progs.append(jellyfin_prog)
    return progs


async def stash_channel_playback_info(ch: dict, item_id: str, request=None) -> JSONResponse:
    """PlaybackInfo for a dynamic Stash channel.

    Returns a MediaSource pointing at our /livetv/channels/{id}/stash-stream
    endpoint, which 302-redirects to Stash's native HLS URL for the current
    scene at the correct seek offset.  IsLive suppresses the scrub bar.
    """
    item_id = item_id.replace("-", "")

    if request is not None:
        base_url = f"{request.url.scheme}://{request.url.netloc}"
    else:
        bind = getattr(config, "PROXY_BIND", "0.0.0.0")
        port = getattr(config, "PROXY_PORT", 8096)
        host = "127.0.0.1" if bind in ("0.0.0.0", "") else bind
        base_url = f"http://{host}:{port}"

    stream_url = f"{base_url}/livetv/channels/{item_id}/stash-stream.m3u8"

    source: dict = {
        "Protocol": "Http",
        "Id": item_id,
        "Type": "Default",
        "Name": ch.get("name", "Live"),
        "IsRemote": True,
        "Path": stream_url,
        "Container": "ts",
        "ReadAtNativeFramerate": True,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsTranscoding": False,
        "SupportsDirectStream": True,
        "SupportsDirectPlay": True,
        "IsInfiniteStream": True,
        "IsLive": True,
        "UseMostCompatibleTranscodingProfile": False,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "RequiresLooping": False,
        "SupportsProbing": False,
        "MediaStreams": [
            {"Type": "Video", "Index": 0, "Codec": "h264",
             "IsDefault": True, "IsExternal": False,
             "IsInterlaced": False, "IsForced": False, "IsHearingImpaired": False,
             "IsTextSubtitleStream": False, "SupportsExternalStream": False},
            {"Type": "Audio", "Index": 1, "Codec": "aac",
             "IsDefault": True, "IsExternal": False, "Channels": 2,
             "IsInterlaced": False, "IsForced": False, "IsHearingImpaired": False,
             "IsTextSubtitleStream": False, "SupportsExternalStream": False},
        ],
        "MediaAttachments": [],
        "Formats": [],
        "RequiredHttpHeaders": {},
        "TranscodingSubProtocol": "hls",
        "HasSegments": False,
        "RunTimeTicks": 0,
    }

    logger.info(f"LiveTV: stash channel playback_info '{ch['name']}' ({item_id}) -> {stream_url}")
    return JSONResponse({
        "MediaSources": [source],
        "PlaySessionId": f"stash_live_{ch['tvg_id']}",
    })


async def endpoint_stash_channel_stream(request: Request):
    """FFmpeg-based live HLS stream for a Stash channel.

    On first play request, spawns an FFmpeg process that:
      • reads a ffconcat playlist of raw Stash scene HTTP streams
        (no Stash-side transcode — byte-range seeking via inpoint)
      • transcodes once to H.264+AAC
      • writes live HLS segments to a per-channel temp directory

    The process is killed automatically after LIVE_TV_IDLE_TIMEOUT seconds
    (default 60 s) of no manifest/segment requests.  Restarted on next play.

    ── Earlier approaches kept for reference ──────────────────────────────
    REDIRECT (best raw quality, no auto-advance):
      # return RedirectResponse(
      #     url=f"{stash_base}/scene/{scene_id}/stream.m3u8?start={seek:.3f}"
      #         + (f"&apikey={api_key}" if api_key else ""),
      #     status_code=302,
      # )

    SLIDING-WINDOW PROXY (auto-advance works, Stash session restarts caused freezes):
      # Fetched Stash m3u8 once per scene_id, served rolling 30-s window of
      # absolute segment URLs, stripped EXT-X-ENDLIST.  Worked until Stash's
      # FFmpeg session expired and old segment URLs became invalid.
    ───────────────────────────────────────────────────────────────────────
    """
    channel_id = request.path_params.get("channel_id", "")
    channel_id_clean = channel_id.replace("-", "")

    ch = _stash_channel_map.get(channel_id) or _stash_channel_map.get(channel_id_clean)
    if not ch:
        await _get_stash_channels()
        ch = _stash_channel_map.get(channel_id) or _stash_channel_map.get(channel_id_clean)
    if not ch:
        logger.warning(f"LiveTV: stash-stream — unknown channel {channel_id}")
        return Response(status_code=404)

    tvg_id = ch["tvg_id"]
    await _ensure_stash_schedules()

    now = time.time()

    if ch.get("stash_type") == "shorts":
        # Shorts: build a ~1-hour on-demand playlist from actual scene files.
        # The EPG shows synthetic 30-minute blocks, so there's no meaningful
        # seek position — we always start a fresh shuffled playlist.
        TARGET_SECS = 3600
        scenes = await _fetch_scenes_for_stash_channel(ch)
        if not scenes:
            logger.warning(f"LiveTV: stash-stream — no scenes for shorts channel")
            return Response(status_code=404)
        pool = list(scenes)
        random.shuffle(pool)
        playlist: list[dict] = []
        total_secs = 0.0
        for s in pool:
            if total_secs >= TARGET_SECS:
                break
            playlist.append({"scene_id": s["id"]})
            total_secs += s["duration_sec"]
        seek = 0.0
        upcoming = playlist
    else:
        schedule = _stash_schedule.get(tvg_id, [])
        # All entries not yet finished — FFmpeg works through them in order.
        upcoming = [e for e in schedule if e["stop_ts"] > now - 5]

        if not upcoming or upcoming[0]["start_ts"] > now + 10:
            logger.warning(f"LiveTV: stash-stream — no current program for {tvg_id}")
            return Response(status_code=404)

        seek = max(0.0, now - upcoming[0]["start_ts"])

    ok = await _ffmpeg_manager.ensure(channel_id_clean, upcoming, seek)
    if not ok:
        return Response(status_code=502, content="FFmpeg failed to start")

    _ffmpeg_manager.touch(channel_id_clean)

    manifest_path = _ffmpeg_manager.manifest_path(channel_id_clean)
    if not manifest_path:
        return Response(status_code=502)

    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return Response(status_code=502)

    # Rewrite relative segment filenames → absolute URLs through our proxy.
    base = f"{request.url.scheme}://{request.url.netloc}"
    out_lines = []
    for line in raw.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out_lines.append(f"{base}/livetv/channels/{channel_id_clean}/seg/{s}")
        else:
            out_lines.append(line)

    logger.trace(
        f"LiveTV FFmpeg: served manifest for '{ch['name']}' "
        f"channel={channel_id_clean} seek={seek:.1f}s entries={len(upcoming)}"
    )
    return Response(
        "\n".join(out_lines),
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-cache, no-store", "Access-Control-Allow-Origin": "*"},
    )


async def endpoint_stash_channel_segment(request: Request):
    """Serve one FFmpeg-generated HLS segment for a live Stash channel."""
    channel_id = request.path_params.get("channel_id", "").replace("-", "")
    seg_name   = request.path_params.get("seg_name",   "")

    if not re.match(r"^seg\d{5}\.ts$", seg_name):
        return Response(status_code=400)

    seg_dir = _ffmpeg_manager.seg_dir(channel_id)
    if not seg_dir:
        return Response(status_code=404)

    seg_path = os.path.join(seg_dir, seg_name)
    if not os.path.exists(seg_path):
        return Response(status_code=404)

    _ffmpeg_manager.touch(channel_id)

    async def _iter():
        with open(seg_path, "rb") as fh:
            while chunk := fh.read(65536):
                yield chunk

    return StreamingResponse(_iter(), media_type="video/mp2t")


# Public lookup API (used by metadata_routes and stream_routes)
# ---------------------------------------------------------------------------

def _normalize_id(item_id: str) -> str:
    """Jellyfin SDK normalizes item IDs to UUID format (with hyphens) before
    putting them in request paths.  Strip hyphens so lookups always work
    regardless of which format arrives."""
    return item_id.replace("-", "")


_STASH_PREFIXES = (b"scene-", b"root-", b"tag-", b"filter-",
                   b"studio-", b"year-", b"person-", b"performer-")

def _is_stash_item(item_id: str) -> bool:
    """Return True if this encoded ID decodes to a known Stash (non-Live TV) prefix.
    Used to silently skip the channel/program lookup for ordinary library items."""
    normalized = _normalize_id(item_id)
    try:
        decoded = bytes.fromhex(normalized[:32].ljust(32, "0"))
        return any(decoded.startswith(p) for p in _STASH_PREFIXES)
    except Exception:
        return False


async def get_channel_by_jellyfin_id(item_id: str) -> dict | None:
    if _is_stash_item(item_id):
        return None
    normalized = _normalize_id(item_id)
    ch = _channel_info_map.get(item_id) or _channel_info_map.get(normalized)
    if ch is not None:
        return ch
    await _get_channels()
    if getattr(config, "ENABLE_STASH_CHANNELS", False):
        await _get_stash_channels()
    ch = _channel_info_map.get(item_id) or _channel_info_map.get(normalized)
    if not ch:
        logger.debug(f"LiveTV: channel lookup MISS for {item_id} (map has {len(_channel_info_map)} entries)")
    return ch


async def get_program_by_jellyfin_id(item_id: str) -> dict | None:
    if _is_stash_item(item_id):
        return None
    normalized = _normalize_id(item_id)
    prog = _program_info_map.get(item_id) or _program_info_map.get(normalized)
    if prog is not None:
        return prog
    await _get_programs()
    prog = _program_info_map.get(item_id) or _program_info_map.get(normalized)
    if not prog:
        logger.debug(f"LiveTV: program lookup MISS for {item_id} (map has {len(_program_info_map)} entries)")
    return prog


# ---------------------------------------------------------------------------
# Jellyfin format helpers
# ---------------------------------------------------------------------------

def _current_program_for(tvg_id: str, programs: list[dict],
                          server_id: str, channels_by_tvg_id: dict) -> dict | None:
    now_ts = time.time()
    for prog in programs:
        if (prog["channel_id"] == tvg_id
                and prog.get("start_ts") and prog.get("stop_ts")
                and prog["start_ts"] <= now_ts <= prog["stop_ts"]):
            return _program_to_jellyfin(prog, server_id, channels_by_tvg_id)
    return None


def _channel_to_jellyfin(ch: dict, server_id: str, item_id: str | None = None,
                          current_program: dict | None = None) -> dict:
    if item_id is None:
        item_id = encode_id("channel", ch["tvg_id"])
    # Id and ItemId must be non-hyphenated (Jellyfin normalizes on the way in but stores raw)
    item_id = item_id.replace("-", "")
    logo = ch.get("logo", "")
    tag = _logo_tag(logo) if logo else ""
    num = ch.get("number", "")
    sort_name = f"{str(num).zfill(5)}.0-{ch['name']}"
    livetv_parent = encode_id("root", "livetv")

    item: dict = {
        "Name": ch["name"],
        "ServerId": server_id,
        "Id": item_id,
        "Etag": hashlib.md5(item_id.encode()).hexdigest(),
        "ChannelId": None,
        "Number": num,
        "ChannelNumber": num,
        "SortName": sort_name,
        "IsFolder": False,
        "Type": "TvChannel",
        "ChannelType": "TV",
        "MediaType": "Video",
        "LocationType": "Remote",
        "PrimaryImageAspectRatio": 1.0,
        "ImageTags": {"Primary": tag} if tag else {},
        "ImageBlurHashes": {},
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": _to_uuid_key(item_id),
            "ItemId": item_id,
        },
        # Full-detail fields (harmless in list context)
        "ParentId": livetv_parent,
        "EnableMediaSourceDisplay": True,
        "PlayAccess": "Full",
        "CanRecord": False,
        "CanDelete": False,
        "CanDownload": False,
        "ExternalUrls": [],
        "ProviderIds": {},
        "People": [],
        "Studios": [],
        "GenreItems": [],
        "Genres": [],
        "Tags": [],
        "Taglines": [],
        "RemoteTrailers": [],
        "MediaStreams": [],
        "LockedFields": [],
        "LockData": False,
        "LocalTrailerCount": 0,
        "SpecialFeatureCount": 0,
        "MediaSources": [
            {
                "Protocol": "File",
                "Id": item_id,
                "Type": "Placeholder",
                "Name": ch["name"],
                "IsRemote": False,
                "ReadAtNativeFramerate": False,
                "IgnoreDts": False,
                "IgnoreIndex": False,
                "GenPtsInput": False,
                "SupportsTranscoding": True,
                "SupportsDirectStream": True,
                "SupportsDirectPlay": True,
                "IsInfiniteStream": True,
                "UseMostCompatibleTranscodingProfile": False,
                "RequiresOpening": False,
                "RequiresClosing": False,
                "RequiresLooping": False,
                "SupportsProbing": True,
                "MediaStreams": [],
                "MediaAttachments": [],
                "Formats": [],
                "RequiredHttpHeaders": {},
                "TranscodingSubProtocol": "http",
                "HasSegments": False,
            }
        ],
    }
    if current_program is not None:
        item["CurrentProgram"] = current_program
    return item


def _program_to_jellyfin(prog: dict, server_id: str, channels_by_tvg_id: dict,
                          prog_id: str | None = None) -> dict:
    ch = channels_by_tvg_id.get(prog["channel_id"], {})
    ch_encoded_id = encode_id("channel", prog["channel_id"])
    if prog_id is None:
        prog_id = encode_id("program", f"{prog['channel_id']}|{prog['start']}")

    ch_logo = ch.get("logo", "")
    ch_tag = _logo_tag(ch_logo) if ch_logo else ""

    icon_url = prog.get("icon", "")
    icon_tag = _logo_tag(icon_url) if icon_url else ""

    # UserData.ItemId must be non-hyphenated; Key must be hyphenated UUID
    prog_id_clean = prog_id.replace("-", "")

    item: dict = {
        "Name": prog["title"],
        "ServerId": server_id,
        "Id": prog_id_clean,
        "ChannelId": ch_encoded_id,
        "ChannelName": ch.get("name", ""),
        "ChannelNumber": ch.get("number", ""),
        "Type": "Program",
        "MediaType": "Video",
        "PlayAccess": "Full",
        "CanRecord": False,
        "StartDate": prog["start"],
        "EndDate": prog["stop"],
        "IsRepeat": True,
        "Tags": ["Repeat"],
        "ImageTags": {"Primary": icon_tag} if icon_tag else {},
        "ImageBlurHashes": {},
        "BackdropImageTags": [],
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": _to_uuid_key(prog_id_clean),
            "ItemId": prog_id_clean,
        },
        "ChannelPrimaryImageTag": ch_tag,
        "ParentId": ch_encoded_id,
        "ExternalUrls": [],
        "ProviderIds": {},
        "People": [],
        "Studios": [],
        "GenreItems": [],
        "Genres": [prog["genre"]] if prog.get("genre") else [],
        "Taglines": [],
        "RemoteTrailers": [],
        "LockedFields": [],
        "LockData": False,
    }

    if icon_tag:
        item["PrimaryImageAspectRatio"] = 1.7777777777777777
    if prog.get("run_time_ticks"):
        item["RunTimeTicks"] = prog["run_time_ticks"]
    if prog.get("year"):
        item["ProductionYear"] = prog["year"]
    if prog.get("desc"):
        item["Overview"] = prog["desc"]

    return item


def channel_playback_info(ch: dict, item_id: str, request=None) -> JSONResponse:
    """PlaybackInfo for a TvChannel.

    Returns our own /livetv/channels/{id}/stream.m3u8 proxy URL so that
    clients (especially ExoPlayer on Android) see a .m3u8 extension and
    automatically select their HLS player.
    """
    item_id = item_id.replace("-", "")

    # Build an absolute proxy URL from the incoming request so the client can
    # reach us.  Falls back to config values when request is unavailable.
    if request is not None:
        base_url = f"{request.url.scheme}://{request.url.netloc}"
    else:
        bind = getattr(config, "PROXY_BIND", "0.0.0.0")
        port = getattr(config, "PROXY_PORT", 8096)
        host = "127.0.0.1" if bind in ("0.0.0.0", "") else bind
        base_url = f"http://{host}:{port}"

    proxy_url = f"{base_url}/livetv/channels/{item_id}/stream.m3u8"
    logger.info(f"LiveTV: channel_playback_info for {ch.get('name')} ({item_id}) -> {proxy_url}")

    source: dict = {
        "Protocol": "Http",
        "Id": item_id,
        "Type": "Default",
        "Name": ch.get("name", "Live"),
        "IsRemote": True,
        "Path": proxy_url,
        "Container": "ts",
        "ReadAtNativeFramerate": True,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsTranscoding": False,
        "SupportsDirectStream": True,
        "SupportsDirectPlay": True,
        "IsInfiniteStream": True,
        "UseMostCompatibleTranscodingProfile": False,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "RequiresLooping": False,
        "SupportsProbing": False,
        "MediaStreams": [
            {"Type": "Video", "Index": 0, "Codec": "h264",
             "IsDefault": True, "IsExternal": False,
             "IsInterlaced": False, "IsForced": False, "IsHearingImpaired": False,
             "IsTextSubtitleStream": False, "SupportsExternalStream": False},
            {"Type": "Audio", "Index": 1, "Codec": "aac",
             "IsDefault": True, "IsExternal": False, "Channels": 2,
             "IsInterlaced": False, "IsForced": False, "IsHearingImpaired": False,
             "IsTextSubtitleStream": False, "SupportsExternalStream": False},
        ],
        "MediaAttachments": [],
        "Formats": [],
        "RequiredHttpHeaders": {},
        "TranscodingSubProtocol": "hls",
        "HasSegments": False,
        "RunTimeTicks": 0,
    }

    return JSONResponse({
        "MediaSources": [source],
        "PlaySessionId": f"live_{item_id}",
        "LiveStreamId": f"live_{item_id}",
    })


async def endpoint_channel_m3u8(request: Request):
    """Proxy the Tunarr HLS playlist through our server.

    Rewrites relative and origin-relative segment URLs to absolute Tunarr URLs
    so clients can fetch segments directly.  Serving the playlist from our
    origin eliminates browser CORS issues; the .m3u8 extension ensures
    ExoPlayer and hls.js select the correct player automatically.
    """
    from urllib.parse import urlparse, urljoin

    channel_id = request.path_params.get("channel_id", "")
    stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        await _get_channels()
        stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        logger.warning(f"LiveTV: m3u8 proxy — no stream URL for channel {channel_id}")
        return Response(status_code=404)

    try:
        resp = await _live_client.get(stream_url, timeout=10.0)
        final_url = str(resp.url)
        if final_url != stream_url:
            logger.info(f"LiveTV: Tunarr redirected {stream_url} -> {final_url}")
        if resp.status_code != 200:
            logger.warning(f"LiveTV: Tunarr returned {resp.status_code} for {final_url}")
            return Response(status_code=resp.status_code)

        resolved_url = final_url
        parsed = urlparse(resolved_url)
        tunarr_origin = f"{parsed.scheme}://{parsed.netloc}"
        base_path = resolved_url.split("?")[0].rsplit("/", 1)[0] + "/"

        lines = []
        for line in resp.text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if stripped.startswith("http://") or stripped.startswith("https://"):
                    lines.append(stripped)
                elif stripped.startswith("/"):
                    lines.append(tunarr_origin + stripped)
                else:
                    lines.append(urljoin(base_path, stripped))
            else:
                lines.append(line)

        logger.info(f"LiveTV: proxied m3u8 for channel {channel_id}")
        return Response(
            content="\n".join(lines),
            media_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache, no-store"},
        )
    except Exception as e:
        logger.error(f"LiveTV: m3u8 proxy failed for channel {channel_id}: {e}")
        return Response(status_code=500)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def endpoint_program_detail(request: Request):
    program_id = request.path_params.get("program_id", "")
    logger.info(f"LiveTV: GET /livetv/programs/{program_id}")
    prog = await get_program_by_jellyfin_id(program_id)
    if prog is None:
        return Response(status_code=404)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    tunarr_channels = await _get_channels() if getattr(config, "ENABLE_TUNARR", False) else []
    stash_channels = await _get_stash_channels() if getattr(config, "ENABLE_STASH_CHANNELS", False) else []
    channels_by_tvg_id = {ch["tvg_id"]: ch for ch in tunarr_channels + stash_channels}
    return JSONResponse(_program_to_jellyfin(prog, server_id, channels_by_tvg_id, program_id))


async def endpoint_timer_defaults(request: Request):
    logger.info("LiveTV: GET /livetv/timers/defaults")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    return JSONResponse({
        "Type": "SeriesTimer",
        "RecordAnyChannel": False,
        "RecordAnyTime": True,
        "RecordNewOnly": False,
        "KeepUntil": "UntilDeleted",
        "Priority": 0,
        "IsPrePaddingRequired": False,
        "IsPostPaddingRequired": False,
        "PrePaddingSeconds": 0,
        "PostPaddingSeconds": 0,
        "SkipEpisodesInLibrary": False,
        "EnabledByDefault": False,
        "ImageTags": {},
        "BackdropImageTags": [],
        "Id": "",
        "ServerId": server_id,
    })


async def endpoint_recordings_folders(request: Request):
    logger.info("LiveTV: GET /livetv/recordings/folders")
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_live_tv_info(request: Request):
    logger.info("LiveTV: GET /livetv/info")
    m3u_url = getattr(config, "TUNER_M3U_URL", "")
    stash_enabled = getattr(config, "ENABLE_STASH_CHANNELS", False)
    services = []
    if getattr(config, "ENABLE_TUNARR", False):
        services.append({
            "Name": "Tunarr Passthrough",
            "HomePageUrl": m3u_url or "",
            "Status": "Running" if m3u_url else "Unavailable",
            "IsVisible": True,
            "HasCancelTimer": False,
            "HasProgramImages": True,
            "HasSeriesTimer": False,
            "CanCreateSeriesTimers": False,
            "CanSetRecordingPath": False,
            "SupportsDirectStreamImport": False,
            "SupportsRecordings": False,
        })
    if stash_enabled:
        services.append({
            "Name": "Stash Dynamic Channels",
            "HomePageUrl": "",
            "Status": "Running",
            "IsVisible": True,
            "HasCancelTimer": False,
            "HasProgramImages": False,
            "HasSeriesTimer": False,
            "CanCreateSeriesTimers": False,
            "CanSetRecordingPath": False,
            "SupportsDirectStreamImport": False,
            "SupportsRecordings": False,
        })
    return JSONResponse({
        "Services": services,
        "IsEnabled": _live_tv_enabled(),
        "HasRecordingSupport": False,
        "EnabledUsers": [],
    })


async def endpoint_channels(request: Request):
    logger.info(f"LiveTV: GET /livetv/channels params={dict(request.query_params)}")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    tunarr_channels = await _get_channels() if getattr(config, "ENABLE_TUNARR", False) else []
    stash_channels = await _get_stash_channels() if getattr(config, "ENABLE_STASH_CHANNELS", False) else []
    all_channels = tunarr_channels + stash_channels
    logger.info(f"LiveTV: returning {len(all_channels)} channels ({len(tunarr_channels)} Tunarr, {len(stash_channels)} Stash)")

    add_current = request.query_params.get("addCurrentProgram", "").lower() == "true"
    tunarr_programs: list[dict] = []
    channels_by_tvg_id: dict = {ch["tvg_id"]: ch for ch in all_channels}
    if add_current and tunarr_channels:
        tunarr_programs = await _get_programs()

    items = []
    for ch in all_channels:
        eid = encode_id("channel", ch["tvg_id"])
        current = None
        if add_current:
            if ch.get("stash_type"):
                now = time.time()
                sched = _stash_schedule.get(ch["tvg_id"], [])
                entry = next((e for e in sched if e["start_ts"] <= now <= e["stop_ts"]), None)
                if entry:
                    raw = {"channel_id": ch["tvg_id"], "title": entry["title"],
                           "start": datetime.fromtimestamp(entry["start_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                           "stop": datetime.fromtimestamp(entry["stop_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                           "start_ts": entry["start_ts"], "stop_ts": entry["stop_ts"],
                           "run_time_ticks": int(entry["duration_sec"] * 10_000_000), "genre": "", "desc": ""}
                    current = _program_to_jellyfin(raw, server_id, channels_by_tvg_id)
            else:
                current = _current_program_for(ch["tvg_id"], tunarr_programs, server_id, channels_by_tvg_id)
        items.append(_channel_to_jellyfin(ch, server_id, eid, current))

    return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})


async def endpoint_programs(request: Request):
    logger.info(f"LiveTV: {request.method} /livetv/programs params={dict(request.query_params)}")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    tunarr_channels = await _get_channels() if getattr(config, "ENABLE_TUNARR", False) else []
    tunarr_programs = await _get_programs() if getattr(config, "ENABLE_TUNARR", False) else []
    stash_channels = await _get_stash_channels() if getattr(config, "ENABLE_STASH_CHANNELS", False) else []

    if getattr(config, "ENABLE_STASH_CHANNELS", False):
        await _ensure_stash_schedules()

    all_channels = tunarr_channels + stash_channels
    channels_by_tvg_id = {ch["tvg_id"]: ch for ch in all_channels}

    # Combine raw program dicts for filtering; Stash programs have a scene_id field
    programs: list[dict] = list(tunarr_programs)
    for ch in stash_channels:
        tvg_id = ch["tvg_id"]
        for entry in _stash_schedule.get(tvg_id, []):
            raw_prog = {
                "channel_id": tvg_id,
                "title": entry["title"],
                "start": datetime.fromtimestamp(entry["start_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                "stop": datetime.fromtimestamp(entry["stop_ts"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                "start_ts": entry["start_ts"],
                "stop_ts": entry["stop_ts"],
                "run_time_ticks": int(entry["duration_sec"] * 10_000_000),
                "genre": "", "desc": "", "scene_id": entry["scene_id"],
                "icon": _stash_screenshot_url(entry["scene_id"]) if entry.get("scene_id") else "",
            }
            prog_id = encode_id("program", f"{tvg_id}|{raw_prog['start']}")
            # Register for single-item lookup (endpoint_program_detail)
            _program_info_map[prog_id.replace("-", "")] = raw_prog
            programs.append(raw_prog)

    # POST body may carry filters as JSON (Wholphin sends POST instead of GET)
    body: dict = {}
    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            body = {}

    def _qp(key: str, default: str = "") -> str:
        """Check query params first, then POST body (case-insensitive)."""
        val = next((v for k, v in request.query_params.items() if k.lower() == key.lower()), None)
        if val is not None:
            return val
        return str(body.get(key, body.get(key.lower(), default)))

    # Channel filter — ChannelIds may be a comma-sep query param or a JSON array in the POST body
    requested: set[str] = set()
    qs_channel_ids = next((v for k, v in request.query_params.items() if k.lower() == "channelids"), None)
    if qs_channel_ids:
        requested = set(qs_channel_ids.split(","))
    else:
        body_ids = body.get("ChannelIds", body.get("channelIds", body.get("channelids")))
        if isinstance(body_ids, list):
            requested = set(body_ids)
        elif isinstance(body_ids, str) and body_ids:
            requested = set(body_ids.split(","))
    # Normalize to unhyphenated hex so hyphenated UUID IDs from clients still match
    requested = {r.replace("-", "") for r in requested}
    logger.info(f"LiveTV: programs channel filter requested={requested or 'ALL'}")
    if requested:
        wanted = {tvg for tvg in channels_by_tvg_id
                  if encode_id("channel", tvg).replace("-", "") in requested}
        logger.info(f"LiveTV: programs channel filter matched tvg_ids={wanted}")
        programs = [p for p in programs if p["channel_id"] in wanted]

    # Time filters
    now_ts = time.time()
    is_airing = _qp("IsAiring", "").lower()
    has_aired = _qp("HasAired", "").lower()

    # Guide time-window filter.  Jellyfin Web sends MaxStartDate/MinEndDate for
    # the visible window.  Clients like Wholphin send neither and get the full
    # schedule — 8000+ items — which overwhelms mobile/TV clients and causes
    # channels (especially shorts) to silently drop from the guide.
    # Default to a 14-hour window (2h past → 12h future) when no params arrive.
    def _parse_guide_ts(raw: str) -> float | None:
        raw = raw.strip()
        if not raw:
            return None
        try:
            raw = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
            return datetime.fromisoformat(raw).timestamp()
        except Exception:
            return None

    max_start_ts = _parse_guide_ts(_qp("MaxStartDate", ""))
    min_end_ts   = _parse_guide_ts(_qp("MinEndDate",   ""))

    # Always apply a server-side cap on the guide window.
    # Clients like Jellyfin Web send a tight window (4-6h) and are unaffected.
    # Clients like Wholphin send the full schedule range (7+ days) or nothing,
    # which produced 8000+ items and caused guide rendering failures.
    GUIDE_FORWARD_CAP = 43_200   # 12h forward
    GUIDE_PAST_CAP    =  7_200   #  2h back

    if max_start_ts is None or max_start_ts > now_ts + GUIDE_FORWARD_CAP:
        max_start_ts = now_ts + GUIDE_FORWARD_CAP
    if min_end_ts is None or min_end_ts < now_ts - GUIDE_PAST_CAP:
        min_end_ts = now_ts - GUIDE_PAST_CAP

    programs = [p for p in programs if p.get("start_ts", 0) <= max_start_ts]
    programs = [p for p in programs if p.get("stop_ts", now_ts) >= min_end_ts]

    if is_airing == "true":
        programs = [p for p in programs
                    if p.get("start_ts") and p.get("stop_ts")
                    and p["start_ts"] <= now_ts <= p["stop_ts"]]
    elif is_airing == "false":
        programs = [p for p in programs
                    if not (p.get("start_ts") and p.get("stop_ts")
                            and p["start_ts"] <= now_ts <= p["stop_ts"])]

    if has_aired == "false":
        programs = [p for p in programs if p.get("stop_ts", 0) > now_ts]
    elif has_aired == "true":
        programs = [p for p in programs if p.get("stop_ts", now_ts + 1) <= now_ts]

    # Pagination
    total = len(programs)
    try:
        start_index = int(_qp("StartIndex", "0"))
    except ValueError:
        start_index = 0
    try:
        limit = int(_qp("Limit", "0"))
    except ValueError:
        limit = 0
    if start_index:
        programs = programs[start_index:]
    if limit:
        programs = programs[:limit]

    items = [_program_to_jellyfin(p, server_id, channels_by_tvg_id) for p in programs]
    logger.info(f"LiveTV: programs returning {len(items)}/{total} items")
    return JSONResponse({"Items": items, "TotalRecordCount": total, "StartIndex": start_index})


async def endpoint_channel_stream(request: Request):
    channel_id = request.path_params.get("channel_id", "")
    stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        await _get_channels()
        stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        return Response(status_code=404)
    return RedirectResponse(url=stream_url, status_code=302)


async def endpoint_guide_info(request: Request):
    now = datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=7)
    return JSONResponse({
        "StartDate": now.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
        "EndDate": end.strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
    })


async def endpoint_recordings(request: Request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_timers(request: Request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_series_timers(request: Request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_rebuild_schedule(request: Request):
    """Force a fresh Stash schedule rebuild — wipes existing data and regenerates."""
    global _stash_schedule, _stash_schedule_built_at
    if not getattr(config, "ENABLE_STASH_CHANNELS", False):
        return JSONResponse({"error": "Stash channels not enabled"}, status_code=400)
    _stash_schedule = {}
    _stash_schedule_built_at = 0.0
    await _rebuild_stash_schedules()
    channel_count = len(_stash_schedule)
    prog_count = sum(len(v) for v in _stash_schedule.values())
    logger.info(f"Schedule rebuild complete: {channel_count} channels, {prog_count} entries")
    return JSONResponse({"ok": True, "channels": channel_count, "programs": prog_count})
