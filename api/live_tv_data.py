import asyncio
import hashlib
import json
import logging
import os
import random
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx

import config
from core.jellyfin_mapper import encode_id, decode_id

logger = logging.getLogger(__name__)

_live_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
CACHE_TTL = 300           # 5-min TTL for M3U/XMLTV/channel-list caches
_SCHEDULE_TTL = 86400.0   # rebuild Stash schedules every 24 h

_m3u_cache: dict = {"data": None, "ts": 0.0}
_xmltv_cache: dict = {"data": None, "ts": 0.0}

_channel_stream_map: dict[str, str] = {}   # encoded_id -> stream_url
_channel_info_map: dict[str, dict] = {}    # encoded_id -> raw channel dict
_channel_tvgid_map: dict[str, str] = {}    # encoded_id (dashless) -> tvg_id (reverse lookup for MD5 hashes)
_program_info_map: dict[str, dict] = {}    # encoded_id -> raw program dict (Tunarr/XMLTV; cleared on every XMLTV refresh)
_program_tvgkey_map: dict[str, tuple] = {} # encoded_id (dashless) -> (channel_id, start) for MD5 hash reversal
_stash_program_map: dict[str, dict] = {}   # encoded_id -> raw program dict (Stash only; never cleared by XMLTV fetch)

# Stash dynamic-channel state
_stash_channels_cache: dict = {"data": None, "ts": 0.0}
_stash_channel_map: dict[str, dict] = {}   # encoded_id -> stash channel dict
_stash_schedule: dict[str, list] = {}      # tvg_id -> sorted list of schedule entries
_stash_schedule_built_at: float = 0.0
_rebuild_lock: asyncio.Lock = asyncio.Lock()

# Persistent channel configuration (channels.json)
_channels_config: list[dict] = []         # ordered list of channel config dicts

def _next_scheduled_segment_after(ch: dict, t: float) -> dict | None:
    """Return the first scheduled segment whose stop_ts > t (i.e. the next
    segment the feeder should play given a wall-clock pointer)."""
    tvg_id = ch["tvg_id"]
    if ch.get("stash_type") == "shorts":
        for block in _stash_schedule.get(tvg_id, []):
            for seg in block.get("segments") or []:
                if float(seg.get("stop_ts", 0)) > t:
                    return seg
    else:
        for e in _stash_schedule.get(tvg_id, []):
            if float(e.get("stop_ts", 0)) > t:
                return e
    return None


def _upcoming_scheduled_segments(ch: dict, after_t: float, count: int = 5) -> list[dict]:
    """Return up to `count` upcoming segments after the given wall-clock
    pointer.  Used by the now-playing modal to show what's queued."""
    tvg_id = ch["tvg_id"]
    out: list[dict] = []
    if ch.get("stash_type") == "shorts":
        for block in _stash_schedule.get(tvg_id, []):
            for seg in block.get("segments") or []:
                if float(seg.get("stop_ts", 0)) > after_t:
                    out.append({
                        "scene_id":     seg.get("scene_id"),
                        "title":        seg.get("title", ""),
                        "duration_sec": float(seg.get("duration_sec") or 0),
                    })
                    if len(out) >= count:
                        return out
    else:
        for e in _stash_schedule.get(tvg_id, []):
            if float(e.get("stop_ts", 0)) > after_t:
                out.append({
                    "scene_id":     e.get("scene_id"),
                    "title":        e.get("title", ""),
                    "duration_sec": float(e.get("duration_sec") or 0),
                })
                if len(out) >= count:
                    return out
    return out


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


def _logo_dir() -> str:
    """Return (and create) the directory where custom channel logos are stored.

    Derives from CONFIG_FILE so Docker deployments always write to /config/channel_logos
    even when LOG_DIR has not been explicitly set.
    """
    config_dir = os.path.dirname(config.CONFIG_FILE)
    d = os.path.join(config_dir, "channel_logos")
    os.makedirs(d, exist_ok=True)
    return d


def _custom_logo_path(tvg_id: str) -> str | None:
    """Return the path to a custom logo file for this channel, or None."""
    if not tvg_id:
        return None
    base = os.path.join(_logo_dir(), tvg_id)
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        p = base + ext
        if os.path.exists(p):
            return p
    return None


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
        logger.notice(f"LiveTV: schedule saved to {path}")
    except Exception as e:
        logger.warning(f"LiveTV: could not save schedule: {e}")


def _load_schedule():
    global _stash_schedule_built_at
    path = _schedule_path()
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        _stash_schedule_built_at = float(payload.get("built_at", 0))
        _stash_schedule.clear()
        _stash_schedule.update(payload.get("schedule", {}))
        # Migration: drop legacy Shorts blocks that lack a `segments` field
        # (older format used synthetic "Shorts" placeholders).  Dropping them
        # forces _ensure_stash_schedules to rebuild with the new structure.
        for tvg_id, entries in list(_stash_schedule.items()):
            if entries and any(e.get("title") == "Shorts" and "segments" not in e for e in entries):
                logger.info(f"LiveTV: dropping legacy Shorts schedule for '{tvg_id}' — will rebuild")
                _stash_schedule.pop(tvg_id, None)
        age_h = (time.time() - _stash_schedule_built_at) / 3600
        logger.notice(f"LiveTV: loaded schedule from disk ({len(_stash_schedule)} channels, {age_h:.1f}h old)")
    except Exception as e:
        logger.warning(f"LiveTV: could not load schedule: {e}")


# ---------------------------------------------------------------------------
# Channel configuration persistence (channels.json)
# ---------------------------------------------------------------------------

def _channels_config_path() -> str:
    log_dir = getattr(config, "LOG_DIR", "/config")
    return os.path.join(log_dir, "channels.json")


def _load_channels_config():
    """Load channel configs from disk; does NOT migrate legacy settings (async)."""
    path = _channels_config_path()
    if not os.path.exists(path):
        logger.info("LiveTV: channels.json not found — will migrate from legacy config on first request")
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _channels_config[:] = data.get("channels", [])
        logger.info(f"LiveTV: loaded {len(_channels_config)} channel configs from disk")
    except Exception as e:
        logger.warning(f"LiveTV: could not load channels.json: {e}")


def _save_channels_config():
    try:
        path = _channels_config_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"channels": _channels_config}, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"LiveTV: could not save channels.json: {e}")


async def _migrate_from_legacy_config() -> list[dict]:
    """One-time migration: build channels.json from STASH_TV_TAGS / STASH_TV_FILTERS."""
    from core import stash_client
    migrated: list[dict] = []
    num = int(getattr(config, "STASH_CHANNEL_START_NUMBER", 5001))

    raw_tags = getattr(config, "STASH_TV_TAGS", "") or ""
    tag_names = [t.strip() for t in (raw_tags.split(",") if isinstance(raw_tags, str) else raw_tags) if str(t).strip()]
    if tag_names:
        all_tags = await stash_client.get_all_tags()
        tags_by_name = {t["name"].lower(): t for t in all_tags}
        for name in tag_names:
            tag = tags_by_name.get(name.lower())
            if tag:
                migrated.append({"tvg_id": f"t{tag['id']}", "name": name,
                                  "number": str(num), "stash_type": "tag",
                                  "source_ids": [tag["id"]], "order": len(migrated)})
                num += 1

    raw_filters = getattr(config, "STASH_TV_FILTERS", "") or ""
    filter_names = [f.strip() for f in (raw_filters.split(",") if isinstance(raw_filters, str) else raw_filters) if str(f).strip()]
    if filter_names:
        saved = await stash_client.get_saved_filters()
        filters_by_name = {f["name"].lower(): f for f in saved}
        for name in filter_names:
            sf = filters_by_name.get(name.lower())
            if sf:
                migrated.append({"tvg_id": f"f{sf['id']}", "name": name,
                                  "number": str(num), "stash_type": "filter",
                                  "source_ids": [sf["id"]], "order": len(migrated)})
                num += 1

    if getattr(config, "ENABLE_SHORTS_CHANNEL", False):
        migrated.append({"tvg_id": "shorts", "name": "Shorts", "number": str(num),
                          "stash_type": "shorts", "source_ids": [], "order": len(migrated)})

    _channels_config[:] = migrated
    if migrated:
        _save_channels_config()
        logger.info(f"LiveTV: migrated {len(migrated)} channels from legacy config to channels.json")
    return migrated


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
            rating_el = prog.find("rating")
            rating = ""
            if rating_el is not None:
                val_el = rating_el.find("value")
                if val_el is not None and val_el.text:
                    rating = val_el.text.strip()
            programs.append({
                "channel_id": prog.get("channel", ""),
                "title": (title_el.text or "Unknown").strip() if title_el is not None else "Unknown",
                "desc": (desc_el.text or "").strip() if desc_el is not None else "",
                "genre": cat_el.text if cat_el is not None else "",
                "year": year,
                "rating": rating,
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
            _channel_tvgid_map.pop(k, None)
        for ch in channels:
            eid = encode_id("ch", ch["tvg_id"])
            _channel_stream_map[eid] = ch["stream_url"]
            _channel_stream_map[eid.replace("-", "")] = ch["stream_url"]
            _channel_info_map[eid] = ch
            _channel_info_map[eid.replace("-", "")] = ch
            _channel_tvgid_map[eid.replace("-", "")] = ch["tvg_id"]
        logger.notice(f"LiveTV: loaded {len(channels)} channels from M3U")
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
        _program_tvgkey_map.clear()
        for prog in programs:
            eid = encode_id("program", f"{prog['channel_id']}|{prog['start']}")
            _program_info_map[eid] = prog
            _program_info_map[eid.replace("-", "")] = prog
            _program_tvgkey_map[eid.replace("-", "")] = (prog["channel_id"], prog["start"])
        logger.notice(f"LiveTV: loaded {len(programs)} programs from XMLTV")
        return programs
    except Exception as e:
        logger.warning(f"LiveTV: failed to fetch XMLTV: {e}")
        return _xmltv_cache["data"] or []


# ---------------------------------------------------------------------------
# Dynamic Stash Channels
# ---------------------------------------------------------------------------

async def _fetch_scenes_for_stash_channel(ch: dict) -> list[dict]:
    """Return [{id, title, duration_sec, …}] for the given Stash channel config.

    Supports multi-source_ids: tag channels union all tag IDs in one query;
    filter channels union results from each saved filter by scene ID.
    """
    from core.stash_client import call_graphql
    channel_type = ch.get("stash_type", "")
    source_ids = [str(s) for s in (ch.get("source_ids") or [])]
    if not source_ids and ch.get("stash_id"):
        source_ids = [str(ch["stash_id"])]

    _SCENE_FIELDS = "id title files { duration } organized rating100 o_counter tags { name } performers { id } details"

    if channel_type == "tag":
        # INCLUDES with multiple IDs = OR — scenes matching ANY of the selected tags
        scene_filter = {"tags": {"value": source_ids, "modifier": "INCLUDES", "depth": 1}}
        query = f"""
        query($sf: SceneFilterType) {{
            findScenes(filter: {{per_page: -1, sort: "id", direction: ASC}}, scene_filter: $sf) {{
                scenes {{ {_SCENE_FIELDS} }}
            }}
        }}
        """
        data = await call_graphql(query, {"sf": scene_filter})
        raw = (data or {}).get("findScenes", {}).get("scenes", [])

    elif channel_type == "filter":
        from core.query_builder import transform_saved_filter
        from core.stash_client import get_saved_filters
        # Union results from all source filters, deduplicating by scene ID
        saved_all = await get_saved_filters()
        saved_by_id = {str(f["id"]): f for f in saved_all}
        raw_by_id: dict[str, dict] = {}
        q = f"""
        query($filter: FindFilterType, $sf: SceneFilterType) {{
            findScenes(filter: $filter, scene_filter: $sf) {{
                scenes {{ {_SCENE_FIELDS} }}
            }}
        }}
        """
        for filter_id in source_ids:
            fd = saved_by_id.get(filter_id)
            if not fd:
                continue
            scene_filter: dict = {}
            filter_args: dict = {"per_page": -1, "sort": "id", "direction": "ASC"}
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
            data = await call_graphql(q, {"filter": filter_args, "sf": scene_filter})
            for s in (data or {}).get("findScenes", {}).get("scenes", []):
                raw_by_id[s["id"]] = s
        raw = list(raw_by_id.values())

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
        raw = (data or {}).get("findScenes", {}).get("scenes", [])
    else:
        raw = []

    shorts_enabled  = bool(getattr(config, "ENABLE_SHORTS_CHANNEL", False))
    shorts_max_secs = int(getattr(config, "SHORTS_MAX_MINUTES", 5)) * 60

    result = []
    for s in raw:
        files = s.get("files") or []
        duration = float(files[0].get("duration") or 0) if files else 0.0
        if duration < 5.0:
            continue
        # When the shorts channel is enabled, exclude short scenes from regular channels
        # so the same scene never appears on both a shorts channel and a regular channel.
        if channel_type != "shorts" and shorts_enabled and duration < shorts_max_secs:
            continue
        result.append({
            "id": s["id"],
            "title": s.get("title") or f"Scene {s['id']}",
            "duration_sec": duration,
            "organized": bool(s.get("organized")),
            "rating": s.get("rating100") or 0,
            "o_counter": s.get("o_counter") or 0,
            "tag_count": len(s.get("tags") or []),
            "tags": [t["name"] for t in (s.get("tags") or [])],
            "performer_count": len(s.get("performers") or []),
            "has_description": bool((s.get("details") or "").strip()),
        })
    return result


def _new_eid() -> str:
    """Generate a compact random entry ID (8 hex chars, ~4 billion space)."""
    return os.urandom(4).hex()


def _scene_genre(scene: dict) -> str:
    """Map Stash scene metadata to a Jellyfin guide color category.

    Priority (first match wins):
        1. Movie  — organized OR rating=100 OR o_counter > 3  → purple
        2. Kids   — tag_count > 3 OR o_counter ≥ 1           → light blue
        3. Sports — tag_count < 3                             → indigo
        4. News   — no description                            → green

    o_counter = Stash "O Counter" (times marked as enjoyed, not watched).
    """
    if (scene.get("organized")
            or (scene.get("rating") or 0) == 100
            or (scene.get("o_counter") or 0) > 3):
        return "Movie"
    tag_count = scene.get("tag_count") or 0
    if tag_count >= 3 or (scene.get("o_counter") or 0) >= 1:
        return "Kids"
    if tag_count < 3:
        return "Sports"
    if not scene.get("has_description"):
        return "News"
    return ""


def _build_random_schedule(scenes: list[dict]) -> list[dict]:
    """Build a full schedule from scratch for a channel.

    Fills [now - KEEP_DAYS, now + SCHEDULE_DAYS].  Scenes cycle through a
    shuffled pool; when exhausted the pool refills so no scene repeats until
    every other scene has aired once.  Each entry receives a stable eid.
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
            "eid": _new_eid(),
            "start_ts": cursor,
            "stop_ts": stop,
            "scene_id": s["id"],
            "title": s["title"],
            "duration_sec": s["duration_sec"],
            "genre": _scene_genre(s),
            "rating": s.get("rating") or 0,
            "o_counter": s.get("o_counter") or 0,
        })
        cursor = stop

    return entries


_HALF_HOUR = 1800  # seconds per Shorts block
_SHORTS_WIGGLE = 120  # max over/undershoot of block boundary, in seconds


def _half_hour_ceil(ts: float) -> float:
    return float(((int(ts) + _HALF_HOUR - 1) // _HALF_HOUR) * _HALF_HOUR)


def _half_hour_floor(ts: float) -> float:
    return float((int(ts) // _HALF_HOUR) * _HALF_HOUR)


def _shorts_blocks_in_range(scenes: list[dict], range_start: float, range_end: float) -> list[dict]:
    """Produce contiguous ~30-minute Shorts blocks between range_start and range_end.

    The first block's start is aligned to the nearest UTC half-hour boundary at or
    after range_start.  Each subsequent block starts exactly where the previous one
    ended — there are no gaps between blocks.  Blocks target _HALF_HOUR in duration
    but may run up to _SHORTS_WIGGLE seconds shorter or longer so that scenes never
    span a block boundary.  If no scene fits within the overshoot budget the block
    closes early and the next block picks up immediately.
    """
    if not scenes:
        return []

    pool = list(scenes)
    random.shuffle(pool)
    pool_idx = 0

    blocks: list[dict] = []
    # First block aligns to the next half-hour boundary; subsequent blocks chain
    # from the actual stop_ts of the preceding block — no gaps.
    block_start = _half_hour_ceil(range_start)
    while block_start < range_end:
        block_end = block_start + _HALF_HOUR
        segments: list[dict] = []
        cursor = block_start
        # Pack scenes greedily; rotate past ones that don't fit.  Stop once we
        # come within _SHORTS_WIGGLE of block_end, or after a full pool sweep
        # without progress.
        attempts = 0
        while attempts < len(pool):
            s = pool[pool_idx % len(pool)]
            pool_idx += 1
            attempts += 1
            if cursor + s["duration_sec"] > block_end + _SHORTS_WIGGLE:
                continue
            segments.append({
                "scene_id":     s["id"],
                "title":        s["title"],
                "start_ts":     cursor,
                "stop_ts":      cursor + s["duration_sec"],
                "duration_sec": s["duration_sec"],
                "genre":        _scene_genre(s),
                "rating":       s.get("rating") or 0,
                "o_counter":    s.get("o_counter") or 0,
            })
            cursor += s["duration_sec"]
            attempts = 0
            if cursor >= block_end - _SHORTS_WIGGLE:
                break

        if segments:
            block_stop = segments[-1]["stop_ts"]
            blocks.append({
                "eid":          _new_eid(),
                "start_ts":     block_start,
                "stop_ts":      block_stop,
                "title":        "Shorts",
                "duration_sec": block_stop - block_start,
                "segments":     segments,
            })
            # Next block starts exactly where this one ended — no gap.
            block_start = block_stop
        else:
            # No scene could fit (pool too short or all scenes exceed wiggle budget).
            # Advance to avoid an infinite loop.
            block_start += _HALF_HOUR

    return blocks


def _build_shorts_block_schedule(scenes: list[dict]) -> list[dict]:
    """Build the full Shorts schedule across the keep/sched window."""
    if not scenes:
        return []
    keep_days  = max(1, int(getattr(config, "STASH_KEEP_DAYS", 2)))
    sched_days = max(1, int(getattr(config, "STASH_SCHEDULE_DAYS", 7)))
    now = time.time()
    return _shorts_blocks_in_range(scenes, now - keep_days * 86400, now + sched_days * 86400)


def _maintenance_extend_channel(
    existing: list[dict],
    all_scenes: list[dict],
    keep_days: int,
    sched_days: int,
) -> tuple[list[dict], int, int]:
    """Prune stale entries and extend a channel's schedule to fill the window.

    Scenes already retained in the schedule are treated as "used" — new
    entries draw from the remaining pool first, cycling through all available
    scenes before any repeats.  Returns (updated_entries, pruned_count, added_count).
    """
    now = time.time()
    cutoff     = now - keep_days * 86400
    target_end = now + sched_days * 86400

    retained  = [e for e in existing if e.get("stop_ts", 0) > cutoff]
    pruned    = len(existing) - len(retained)
    frontier  = max((e["stop_ts"] for e in retained), default=cutoff)

    if frontier >= target_end:
        return retained, pruned, 0

    # Scenes in the retained schedule are "used"; everything else is available.
    used_ids  = {e["scene_id"] for e in retained if e.get("scene_id")}
    available = [s for s in all_scenes if s["id"] not in used_ids]
    random.shuffle(available)
    used      = [s for s in all_scenes if s["id"] in used_ids]

    if not available:
        # Every scene is already scheduled — start a fresh cycle.
        available = list(all_scenes)
        random.shuffle(available)
        used = []

    new_entries: list[dict] = []
    cursor = frontier
    while cursor < target_end:
        if not available:
            available = used
            random.shuffle(available)
            used = []
        s = available.pop()
        used.append(s)
        stop = cursor + s["duration_sec"]
        new_entries.append({
            "eid":          _new_eid(),
            "start_ts":     cursor,
            "stop_ts":      stop,
            "scene_id":     s["id"],
            "title":        s["title"],
            "duration_sec": s["duration_sec"],
            "genre":        _scene_genre(s),
            "rating":       s.get("rating") or 0,
            "o_counter":    s.get("o_counter") or 0,
        })
        cursor = stop

    return retained + new_entries, pruned, len(new_entries)


def _maintenance_extend_shorts(
    existing: list[dict],
    scenes: list[dict],
    keep_days: int,
    sched_days: int,
) -> tuple[list[dict], int, int]:
    """Prune stale Shorts blocks and append new ones to fill the window."""
    now        = time.time()
    cutoff     = now - keep_days * 86400
    target_end = now + sched_days * 86400

    retained = [b for b in existing if b.get("stop_ts", 0) > cutoff]
    pruned   = len(existing) - len(retained)

    # Chain new blocks from the actual end of the last retained block so there
    # is no gap between the retained schedule and the newly generated content.
    if retained:
        frontier = max(b["stop_ts"] for b in retained)
    else:
        frontier = cutoff

    new_blocks = _shorts_blocks_in_range(scenes, frontier, target_end)
    return retained + new_blocks, pruned, len(new_blocks)


async def _get_stash_channels() -> list[dict]:
    """Build runtime channel list from channels.json config, migrating from legacy settings if needed."""
    from core import stash_client
    now = time.time()
    if _stash_channels_cache["data"] is not None and now - _stash_channels_cache["ts"] < CACHE_TTL:
        return _stash_channels_cache["data"]

    # First run: migrate from STASH_TV_TAGS / STASH_TV_FILTERS if no channels.json exists
    if not _channels_config and not os.path.exists(_channels_config_path()):
        await _migrate_from_legacy_config()

    configs = sorted(_channels_config, key=lambda c: c.get("order", 0))

    # Pre-fetch tag images for tag channels (single batch call)
    tag_info: dict[str, dict] = {}
    if any(c.get("stash_type") == "tag" for c in configs):
        all_tags = await stash_client.get_all_tags()
        tag_info = {t["id"]: t for t in all_tags}

    channels: list[dict] = []
    for cfg in configs:
        stash_type = cfg.get("stash_type", "tag")
        source_ids  = cfg.get("source_ids") or []

        logo = ""
        if stash_type == "tag" and source_ids:
            raw_logo = tag_info.get(str(source_ids[0]), {}).get("image_path", "")
            if raw_logo:
                if not raw_logo.startswith("http"):
                    raw_logo = f"{config.get_stash_base()}{raw_logo}"
                api_key = getattr(config, "STASH_API_KEY", "")
                if api_key and "apikey=" not in raw_logo:
                    raw_logo += f"{'&' if '?' in raw_logo else '?'}apikey={api_key}"
                logo = raw_logo

        ch: dict = {
            "tvg_id": cfg["tvg_id"],
            "name": cfg.get("name", "Channel"),
            "number": cfg.get("number", "5001"),
            "logo": logo,
            "stash_type": stash_type,
            "source_ids": source_ids,
            # Legacy compat: single stash_id field (first source)
            "stash_id": str(source_ids[0]) if source_ids else "",
        }
        channels.append(ch)

    _stash_channels_cache["data"] = channels
    _stash_channels_cache["ts"] = now

    for ch in channels:
        enc = encode_id("ch", ch["tvg_id"])
        _channel_info_map[enc] = ch
        _channel_info_map[enc.replace("-", "")] = ch
        _stash_channel_map[enc] = ch
        _stash_channel_map[enc.replace("-", "")] = ch
        _channel_tvgid_map[enc.replace("-", "")] = ch["tvg_id"]

    logger.info(f"LiveTV: {len(channels)} Stash channels configured")
    return channels


async def _rebuild_stash_schedules():
    """Fetch scenes for every Stash channel and rebuild all schedules."""
    global _stash_schedule_built_at

    if not getattr(config, "ENABLE_STASH_CHANNELS", False):
        return

    async with _rebuild_lock:
        channels = await _get_stash_channels()
        new_schedule: dict[str, list] = {}

        for ch in channels:
            tvg_id = ch["tvg_id"]
            try:
                if ch.get("stash_type") == "shorts":
                    scenes = await _fetch_scenes_for_stash_channel(ch)
                    if not scenes:
                        logger.warning(f"LiveTV: no scenes for channel '{ch['name']}' — EPG will be empty")
                        continue
                    slots = _build_shorts_block_schedule(scenes)
                    new_schedule[tvg_id] = slots
                    seg_total = sum(len(b.get("segments", [])) for b in slots)
                    logger.notice(f"LiveTV: schedule built for '{ch['name']}' — {len(scenes)} scenes, {len(slots)} 30-min blocks, {seg_total} segments")
                else:
                    scenes = await _fetch_scenes_for_stash_channel(ch)
                    if not scenes:
                        logger.warning(f"LiveTV: no scenes for channel '{ch['name']}' — EPG will be empty")
                        continue
                    slots = _build_random_schedule(scenes)
                    new_schedule[tvg_id] = slots
                    logger.notice(f"LiveTV: schedule built for '{ch['name']}' — {len(scenes)} scenes, {len(slots)} EPG slots")
            except Exception as e:
                logger.error(f"LiveTV: schedule build failed for '{ch['name']}': {e}", exc_info=True)

        _stash_schedule.clear()
        _stash_schedule.update(new_schedule)
        _stash_schedule_built_at = time.time()
        _save_schedule()


async def _run_maintenance_update():
    """Prune old entries and extend each channel's schedule forward.

    Preserves all existing entries within the keep-days window so users see
    the same schedule they already looked at.  Only prunes the past and
    appends new content at the end.
    """
    global _stash_schedule_built_at

    if not getattr(config, "ENABLE_STASH_CHANNELS", False):
        return

    async with _rebuild_lock:
        channels  = await _get_stash_channels()
        keep_days = max(1, int(getattr(config, "STASH_KEEP_DAYS", 2)))
        sched_days = max(1, int(getattr(config, "STASH_SCHEDULE_DAYS", 7)))
        changed   = False

        for ch in channels:
            tvg_id   = ch["tvg_id"]
            existing = _stash_schedule.get(tvg_id, [])
            try:
                scenes = await _fetch_scenes_for_stash_channel(ch)
                if ch.get("stash_type") == "shorts":
                    # Shorts still prunes past blocks even when scenes is empty
                    # (channel temporarily without content); extend is just a no-op.
                    updated, pruned, added = _maintenance_extend_shorts(existing, scenes, keep_days, sched_days)
                else:
                    if not scenes:
                        continue
                    updated, pruned, added = _maintenance_extend_channel(existing, scenes, keep_days, sched_days)

                if pruned or added:
                    logger.notice(
                        f"LiveTV maintenance: '{ch['name']}' pruned={pruned} added={added} "
                        f"total={len(updated)}"
                    )
                _stash_schedule[tvg_id] = updated
                changed = True
            except Exception as e:
                logger.error(f"LiveTV maintenance: failed for '{ch['name']}': {e}", exc_info=True)

        if changed:
            _stash_schedule_built_at = time.time()
            _save_schedule()


async def _ensure_stash_schedules():
    """Bootstrap or refresh schedules on first request.

    - No schedule at all → full rebuild (first run or after a manual clear).
    - Schedule loaded from disk but older than TTL → incremental maintenance
      (preserves existing entries, only prunes + extends).
    """
    if not _stash_schedule:
        await _rebuild_stash_schedules()
    elif time.time() - _stash_schedule_built_at > _SCHEDULE_TTL:
        await _run_maintenance_update()


# ---------------------------------------------------------------------------
# Background maintenance task
# ---------------------------------------------------------------------------

_maintenance_task: asyncio.Task | None = None


async def _schedule_maintenance_loop():
    """Prune old entries and extend the schedule window — runs every 24 hours."""
    while True:
        await asyncio.sleep(_SCHEDULE_TTL)
        logger.info("LiveTV: 24h maintenance — pruning old entries and extending schedule window")
        try:
            await _run_maintenance_update()
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
            "genre": entry.get("genre", ""),
            "desc": "",
            "scene_id": entry["scene_id"],
            "icon": _stash_screenshot_url(entry["scene_id"]) if entry.get("scene_id") else "",
        }
        prog_id = encode_id("program", f"{tvg_id}|{raw_prog['start']}")
        jellyfin_prog = _program_to_jellyfin(raw_prog, server_id, channels_by_tvg_id, prog_id)
        enc = prog_id.replace("-", "")
        _program_info_map[enc] = raw_prog
        progs.append(jellyfin_prog)
    return progs


async def _build_stash_channel_playlist(ch: dict) -> tuple[list[dict], float] | None:
    """Return (entries, seek_seconds) for the channel's current content, or None.

    Shared by stash_channel_playback_info (pre-start) and
    endpoint_stash_channel_stream (manifest serve).
    """
    tvg_id = ch["tvg_id"]
    now = time.time()

    if ch.get("stash_type") == "shorts":
        # Flatten the segments stored in each block into a single schedule and
        # play it the same way as a regular channel — seek into the segment
        # currently airing, then queue everything after it.  If we land in a
        # small wiggle gap between two blocks the first upcoming segment may
        # start slightly in the future; play it from its beginning rather than
        # failing so the user doesn't see a stall.
        blocks = _stash_schedule.get(tvg_id, [])
        all_segments: list[dict] = []
        for block in blocks:
            all_segments.extend(block.get("segments") or [])
        upcoming = [s for s in all_segments if s["stop_ts"] > now - 5]
        if not upcoming:
            return None
        playlist: list[dict] = []
        total = 0.0
        for s in upcoming:
            if total >= 3600:
                break
            playlist.append({
                "scene_id":     s["scene_id"],
                "title":        s.get("title", ""),
                "duration_sec": s["duration_sec"],
            })
            total += s["duration_sec"]
        return playlist, max(0.0, now - upcoming[0]["start_ts"])
    else:
        schedule = _stash_schedule.get(tvg_id, [])
        upcoming = [e for e in schedule if e["stop_ts"] > now - 5]
        if not upcoming or upcoming[0]["start_ts"] > now + 10:
            return None
        return upcoming, max(0.0, now - upcoming[0]["start_ts"])
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
    from core.jellyfin_mapper import decode_id
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
    if ch is not None:
        return ch
    # Fallback: decode the ID — if it resolves to "ch-{tvg_id}", look up by tvg_id
    # directly. Handles any edge case where the encoded ID isn't in the map yet.
    decoded = decode_id(item_id)
    if decoded.startswith("ch-"):
        tvg_id = decoded[3:]
        tunarr = _m3u_cache.get("data") or []
        stash = _stash_channels_cache.get("data") or []
        ch = next((c for c in tunarr + stash if c.get("tvg_id") == tvg_id), None)
        if ch:
            logger.debug(f"LiveTV: channel lookup via decoded tvg_id {tvg_id!r}")
            return ch
    # Ultimate fallback: registry lookup for MD5-hashed IDs (long tvg_ids that exceed the
    # 32-char hex limit and can't be reversed through decode_id).
    tvg_id = _channel_tvgid_map.get(normalized)
    if tvg_id:
        tunarr = _m3u_cache.get("data") or []
        stash = _stash_channels_cache.get("data") or []
        ch = next((c for c in tunarr + stash if c.get("tvg_id") == tvg_id), None)
        if ch:
            logger.debug(f"LiveTV: channel lookup via registry for {normalized!r} → tvg_id {tvg_id!r}")
            return ch
    logger.debug(f"LiveTV: channel lookup MISS for {item_id} (map has {len(_channel_info_map)} entries)")
    return None


async def get_program_by_jellyfin_id(item_id: str) -> dict | None:
    if _is_stash_item(item_id):
        return None
    normalized = _normalize_id(item_id)

    # 1. Fast path: Stash-specific map (never cleared by XMLTV refreshes)
    prog = _stash_program_map.get(normalized)
    logger.debug(f"LiveTV: program lookup L1 for {normalized}: {'HIT' if prog is not None else f'MISS (map has {len(_stash_program_map)} entries)'}")
    if prog is not None:
        return prog

    # 2. Shared map (Tunarr programs + any Stash entries not yet cleared)
    prog = _program_info_map.get(item_id) or _program_info_map.get(normalized)
    if prog is not None:
        return prog

    # 3. Search _stash_schedule directly — handles cold-start where endpoint_programs
    #    hasn't been called yet and _stash_program_map is empty.
    if _stash_schedule:
        from datetime import datetime as _dt, timezone as _tz
        for tvg_id, schedule in _stash_schedule.items():
            for entry in schedule:
                start_str = _dt.fromtimestamp(entry["start_ts"], _tz.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
                pid = encode_id("program", f"{tvg_id}|{start_str}").replace("-", "")
                if pid == normalized:
                    raw_prog = {
                        "channel_id": tvg_id,
                        "title": entry["title"],
                        "start": start_str,
                        "stop": _dt.fromtimestamp(entry["stop_ts"], _tz.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                        "start_ts": entry["start_ts"],
                        "stop_ts": entry["stop_ts"],
                        "run_time_ticks": int(entry["duration_sec"] * 10_000_000),
                        "genre": entry.get("genre", ""), "desc": "",
                        "scene_id": entry.get("scene_id"),
                        "icon": _stash_screenshot_url(entry["scene_id"]) if entry.get("scene_id") else "",
                    }
                    _stash_program_map[normalized] = raw_prog  # cache for future lookups
                    return raw_prog

    # 4. Fall back to XMLTV/Tunarr refresh (does not affect _stash_program_map)
    await _get_programs()
    prog = _program_info_map.get(item_id) or _program_info_map.get(normalized)
    if prog:
        return prog
    # 5. Registry lookup for MD5-hashed program IDs (long channel_id|start strings).
    key = _program_tvgkey_map.get(normalized)
    if key:
        channel_id, start = key
        programs = _xmltv_cache.get("data") or []
        prog = next((p for p in programs if p.get("channel_id") == channel_id and p.get("start") == start), None)
        if prog:
            logger.debug(f"LiveTV: program lookup via registry for {normalized!r} → {channel_id}|{start}")
            return prog
    logger.debug(f"LiveTV: program lookup MISS for {item_id} (map has {len(_program_info_map)} entries)")
    return None


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
        item_id = encode_id("ch", ch["tvg_id"])
    # Id and ItemId must be non-hyphenated (Jellyfin normalizes on the way in but stores raw)
    item_id = item_id.replace("-", "")
    logo = ch.get("logo", "")
    custom = _custom_logo_path(ch.get("tvg_id", ""))
    if custom:
        tag = hashlib.md5(f"custom:{ch['tvg_id']}:{os.path.getmtime(custom):.0f}".encode()).hexdigest()
    elif logo:
        tag = _logo_tag(logo)
    else:
        tag = ""
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
    ch_encoded_id = encode_id("ch", prog["channel_id"])
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
        # Boolean type flags — Jellyfin Web and Wholphin use these (not Genres) for EPG color coding.
        "IsMovie":  prog.get("genre") == "Movie",
        "IsKids":   prog.get("genre") == "Kids",
        "IsSports": prog.get("genre") == "Sports",
        "IsNews":   prog.get("genre") == "News",
        "IsSeries": prog.get("genre") not in ("Movie", ""),
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

