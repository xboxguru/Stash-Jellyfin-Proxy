import asyncio
import glob
import logging
import mimetypes
import os
import re
import time
from datetime import date as _date_cls, datetime, timezone

import config
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from core.jellyfin_mapper import encode_id
from core.stash_client import get_all_tags, get_saved_filters
import api.live_tv_data as _d
from api.live_tv_data import (
    _save_schedule, _save_channels_config, _load_channels_config,
    _ensure_stash_schedules, _get_stash_channels, _fetch_scenes_for_stash_channel,
    _build_random_schedule, _build_shorts_block_schedule, _live_tv_enabled,
    _logo_dir, _custom_logo_path, _stash_screenshot_url, _new_eid,
    _upcoming_scheduled_segments, _rebuild_stash_schedules,
    _stash_channels_cache, _m3u_cache, _live_client,
    _channels_config, _stash_schedule,
    _rebuild_lock, _get_channels, _get_programs, _scene_genre,
)

logger = logging.getLogger(__name__)
async def _rebuild_single_channel(tvg_id: str):
    """Rebuild (wipe + regenerate) the schedule for one channel."""
    channels = await _get_stash_channels()
    ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if not ch:
        logger.warning(f"LiveTV: single-channel rebuild — unknown channel {tvg_id}")
        return
    async with _rebuild_lock:
        try:
            scenes = await _fetch_scenes_for_stash_channel(ch)
            if not scenes:
                logger.warning(f"LiveTV: no scenes for '{ch['name']}' — schedule will be empty")
                return
            if ch.get("stash_type") == "shorts":
                _stash_schedule[tvg_id] = _build_shorts_block_schedule(scenes)
            else:
                _stash_schedule[tvg_id] = _build_random_schedule(scenes)
            logger.info(f"LiveTV: rebuilt schedule for '{ch['name']}' — {len(scenes)} scenes")
            _d._stash_schedule_built_at = time.time()
            _save_schedule()
        except Exception as e:
            logger.error(f"LiveTV: single-channel rebuild failed for '{ch['name']}': {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Channel config CRUD endpoints
# ---------------------------------------------------------------------------

async def endpoint_stash_tags_list(request: Request):
    """Return all Stash tags that have at least one scene."""
    from core.stash_client import get_all_tags
    tags = await get_all_tags()
    return JSONResponse({"tags": [{"id": t["id"], "name": t["name"], "has_image": bool(t.get("image_path"))} for t in tags]})


async def endpoint_stash_tag_image(request: Request):
    """Proxy a Stash tag's image through the server."""
    tag_id = request.path_params.get("tag_id", "")
    if not re.match(r'^\d+$', tag_id):
        return Response(status_code=400)
    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    url = f"{stash_base}/tag/{tag_id}/image"
    if apikey:
        url += f"?apikey={apikey}"
    from api.image_routes import _proxy_image
    return await _proxy_image(url)


async def endpoint_channel_logo_set_from_tag(request: Request):
    """Download a Stash tag's image and save it as the channel logo."""
    tvg_id = request.path_params.get("tvg_id", "")
    if not re.match(r'^[a-zA-Z0-9_-]{1,60}$', tvg_id):
        return JSONResponse({"error": "invalid tvg_id"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    tag_id = str(body.get("tag_id", "")).strip()
    if not re.match(r'^\d+$', tag_id):
        return JSONResponse({"error": "invalid tag_id"}, status_code=400)

    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    image_url = f"{stash_base}/tag/{tag_id}/image"
    if apikey:
        image_url += f"?apikey={apikey}"

    from api.image_routes import image_client
    try:
        r = await image_client.get(image_url)
        if r.status_code != 200:
            return JSONResponse({"error": "failed to fetch tag image"}, status_code=502)
        content = r.content
        ct = r.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)

    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
    ext = ext_map.get(ct, ".jpg")
    logo_dir = _logo_dir()
    for existing in glob.glob(os.path.join(logo_dir, f"{tvg_id}.*")):
        os.remove(existing)
    dest = os.path.join(logo_dir, f"{tvg_id}{ext}")
    with open(dest, "wb") as fh:
        fh.write(content)
    _stash_channels_cache["data"] = None
    logger.info(f"LiveTV: tag {tag_id} image saved as logo for '{tvg_id}' → {dest}")
    return JSONResponse({"ok": True})


async def endpoint_stash_filters_list(request: Request):
    """Return all Stash saved scene filters."""
    from core.stash_client import get_saved_filters
    filters = await get_saved_filters()
    return JSONResponse({"filters": [{"id": f["id"], "name": f["name"]} for f in filters]})


async def endpoint_channels_config_list(request: Request):
    """Return ordered channel config list."""
    return JSONResponse({"channels": sorted(_channels_config, key=lambda c: c.get("order", 0))})


async def endpoint_channels_config_create(request: Request):
    """Create a new channel and immediately fire its schedule build in the background."""
    body = await request.json()
    name       = str(body.get("name", "")).strip()
    stash_type = str(body.get("stash_type", "tag"))
    source_ids = [str(s) for s in (body.get("source_ids") or [])]
    triptych   = bool(body.get("triptych", False))
    is_shorts  = stash_type == "shorts"
    # source_ids required unless it's shorts or triptych
    if not name or (not is_shorts and not triptych and not source_ids):
        return JSONResponse({"error": "name is required; source_ids required unless using Shorts or Triptych"}, status_code=400)

    # Auto-assign next available channel number
    used_numbers = {int(c["number"]) for c in _channels_config if str(c.get("number", "")).isdigit()}
    start = int(getattr(config, "STASH_CHANNEL_START_NUMBER", 5001))
    requested = body.get("number")
    if requested and str(requested).isdigit():
        number = str(int(requested))
    else:
        n = start
        while n in used_numbers:
            n += 1
        number = str(n)

    tvg_id = "ch_" + os.urandom(4).hex()
    new_cfg = {"tvg_id": tvg_id, "name": name, "number": number,
               "stash_type": stash_type, "source_ids": source_ids,
               "triptych": triptych, "triptych_salt": str(body.get("triptych_salt", "")).strip(),
               "order": len(_channels_config)}
    _channels_config.append(new_cfg)
    _save_channels_config()
    _stash_channels_cache["data"] = None
    _stash_channels_cache["ts"] = 0.0

    # Build the schedule in the background so the response is immediate
    asyncio.create_task(_rebuild_single_channel(tvg_id))
    return JSONResponse({"ok": True, "channel": new_cfg}, status_code=201)


async def endpoint_channels_config_update(request: Request):
    """Update channel metadata (name, number, sources).  Does NOT rebuild the schedule."""
    tvg_id = request.path_params.get("tvg_id", "")
    idx = next((i for i, c in enumerate(_channels_config) if c["tvg_id"] == tvg_id), None)
    if idx is None:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    body = await request.json()
    cfg  = _channels_config[idx]
    if "name"       in body: cfg["name"]       = str(body["name"]).strip()
    if "number"     in body: cfg["number"]      = str(body["number"])
    if "stash_type" in body: cfg["stash_type"]  = str(body["stash_type"])
    if "source_ids" in body: cfg["source_ids"]  = [str(s) for s in body["source_ids"]]
    if "triptych"   in body: cfg["triptych"]    = bool(body["triptych"])
    if "triptych_salt" in body: cfg["triptych_salt"] = str(body["triptych_salt"]).strip()
    _channels_config[idx] = cfg
    _save_channels_config()
    _stash_channels_cache["data"] = None
    _stash_channels_cache["ts"] = 0.0
    return JSONResponse({"ok": True, "channel": cfg})


async def endpoint_channels_config_delete(request: Request):
    """Delete a channel and its schedule."""
    tvg_id = request.path_params.get("tvg_id", "")
    before = len(_d._channels_config)
    _d._channels_config = [c for c in _d._channels_config if c["tvg_id"] != tvg_id]
    if len(_d._channels_config) == before:
        return JSONResponse({"error": "channel not found"}, status_code=404)
    for i, c in enumerate(_d._channels_config):
        c["order"] = i
    _d._stash_schedule.pop(tvg_id, None)
    _save_channels_config()
    _save_schedule()
    _stash_channels_cache["data"] = None
    _stash_channels_cache["ts"] = 0.0
    # Remove custom logo if present
    custom = _custom_logo_path(tvg_id)
    if custom and os.path.exists(custom):
        try: os.remove(custom)
        except Exception: pass
    return JSONResponse({"ok": True})


def _renumber_after_reorder(new_order: list[dict], src_id: str, src_old_idx: int) -> None:
    """Assign a new channel number to the moved channel and cascade-shift displaced channels.

    Algorithm:
    1. Look at the new neighbors (above/below in list order) and check if a free
       integer exists in the gap between their numbers → assign it, done.
    2. Otherwise sequential-cascade: collect the numbers of all channels in the
       affected index range (src's old..new positions, inclusive), sort them, then
       assign them in ascending order to those channels sorted by new position.
       This is equivalent to the displaced channels each taking the number from
       their neighbor toward src's origin, cascading until src's vacated slot is
       consumed.
    """
    src_new_idx = next((i for i, c in enumerate(new_order) if c["tvg_id"] == src_id), None)
    if src_new_idx is None or src_new_idx == src_old_idx:
        return

    def _to_int(c: dict) -> int:
        try:
            return int(c.get("number", 0))
        except (ValueError, TypeError):
            return 0

    # All occupied numbers except src's old number (src vacated it)
    src_old_num = _to_int(new_order[src_new_idx])
    occupied = {_to_int(c) for c in new_order if c["tvg_id"] != src_id}

    # Neighbors in new order (using old numbers, not yet reassigned)
    above_num = _to_int(new_order[src_new_idx - 1]) if src_new_idx > 0 else None
    below_num = _to_int(new_order[src_new_idx + 1]) if src_new_idx < len(new_order) - 1 else None

    # Step 1: gap check
    lo_bound = above_num if above_num is not None else 0
    hi_bound = below_num if below_num is not None else lo_bound + 2
    for n in range(lo_bound + 1, hi_bound):
        if n not in occupied:
            new_order[src_new_idx]["number"] = str(n)
            return

    # Step 2: cascade via sequential assignment of affected range
    lo = min(src_old_idx, src_new_idx)
    hi = max(src_old_idx, src_new_idx)
    affected = new_order[lo:hi + 1]   # channels at these new-order positions
    nums = sorted(_to_int(c) for c in affected)
    for i, c in enumerate(affected):
        c["number"] = str(nums[i])


async def endpoint_channels_config_reorder(request: Request):
    """Accept an ordered list of tvg_ids and persist the new sort order + renumber."""
    body = await request.json()
    ordered_ids: list[str] = [str(x) for x in (body.get("order") or [])]
    src_id: str = str(body.get("src_id", ""))

    # Remember src's old position before reordering
    old_index = {c["tvg_id"]: i for i, c in enumerate(_d._channels_config)}
    src_old_idx = old_index.get(src_id, -1)

    cfg_by_id = {c["tvg_id"]: c for c in _d._channels_config}
    new_order: list[dict] = []
    for i, tid in enumerate(ordered_ids):
        if tid in cfg_by_id:
            cfg_by_id[tid]["order"] = i
            new_order.append(cfg_by_id[tid])
    # Append anything not in the submitted list (shouldn't normally happen)
    present = {c["tvg_id"] for c in new_order}
    for c in _d._channels_config:
        if c["tvg_id"] not in present:
            c["order"] = len(new_order)
            new_order.append(c)

    # Renumber the moved channel (and cascade-shift displaced ones)
    if src_id and src_old_idx >= 0:
        _renumber_after_reorder(new_order, src_id, src_old_idx)

    _d._channels_config = new_order
    _save_channels_config()
    _stash_channels_cache["data"] = None
    _stash_channels_cache["ts"] = 0.0
    return JSONResponse({"ok": True})


async def endpoint_channel_rebuild(request: Request):
    """Wipe and rebuild the schedule for a single channel."""
    tvg_id = request.path_params.get("tvg_id", "")
    if not any(c["tvg_id"] == tvg_id for c in _channels_config):
        return JSONResponse({"error": "channel not found"}, status_code=404)
    _stash_schedule.pop(tvg_id, None)
    await _rebuild_single_channel(tvg_id)
    return JSONResponse({"ok": True, "programs": len(_stash_schedule.get(tvg_id, []))})


async def endpoint_rebuild_schedule(request: Request):
    """Force a fresh Stash schedule rebuild — wipes existing data and regenerates."""
    if not getattr(config, "ENABLE_STASH_CHANNELS", False):
        return JSONResponse({"error": "Stash channels not enabled"}, status_code=400)
    _d._stash_schedule.clear()
    _d._stash_schedule_built_at = 0.0
    await _rebuild_stash_schedules()
    channel_count = len(_d._stash_schedule)
    prog_count = sum(len(v) for v in _d._stash_schedule.values())
    logger.info(f"Schedule rebuild complete: {channel_count} channels, {prog_count} entries")
    return JSONResponse({"ok": True, "channels": channel_count, "programs": prog_count})


async def endpoint_guide_data(request: Request):
    """Return full-day EPG data for all channels (Tunarr + Stash).

    Query params:
        date (optional): YYYY-MM-DD in UTC; defaults to today UTC.
    """
    # Prefer a Unix timestamp sent by the browser (local midnight in the user's
    # timezone).  Fall back to a YYYY-MM-DD date string interpreted as UTC midnight,
    # then to today UTC midnight.
    ts_str   = request.query_params.get("ts", "")
    date_str = request.query_params.get("date", "")
    try:
        if ts_str:
            day_start = int(float(ts_str))
        elif date_str:
            d = _date_cls.fromisoformat(date_str)
            day_start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        else:
            d = _date_cls.today()
            day_start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    except Exception:
        return JSONResponse({"error": "invalid date"}, status_code=400)

    day_end = day_start + 86400
    date_label = datetime.fromtimestamp(day_start, tz=timezone.utc).strftime("%Y-%m-%d")

    tunarr_enabled = getattr(config, "ENABLE_TUNARR", False)
    stash_enabled  = getattr(config, "ENABLE_STASH_CHANNELS", False)

    if not tunarr_enabled and not stash_enabled:
        return JSONResponse({"date": date_label, "day_start": day_start, "day_end": day_end, "channels": []})

    result: list[dict] = []

    # ── Tunarr / Ersatz channels (read-only — managed externally) ──────────
    if tunarr_enabled:
        tunarr_channels = await _get_channels()
        tunarr_programs = await _get_programs()
        programs_by_channel: dict[str, list] = {}
        for prog in tunarr_programs:
            programs_by_channel.setdefault(prog["channel_id"], []).append(prog)

        for ch in tunarr_channels:
            tvg_id = ch["tvg_id"]
            programs = []
            for prog in programs_by_channel.get(tvg_id, []):
                if prog.get("stop_ts", 0) <= day_start or prog.get("start_ts", 0) >= day_end:
                    continue
                programs.append({
                    "eid": "",
                    "start_ts": prog["start_ts"],
                    "stop_ts": prog["stop_ts"],
                    "title": prog["title"],
                    "scene_id": None,
                    "genre": prog.get("genre", ""),
                    "desc": prog.get("desc", ""),
                    "year": prog.get("year"),
                    "rating": prog.get("rating", ""),
                    "icon_url": prog.get("icon", ""),
                })
            custom = _custom_logo_path(tvg_id)
            if custom:
                logo_url = f"/api/livetv/channel-logo/{tvg_id}?v={int(os.path.getmtime(custom))}"
            elif ch.get("logo"):
                logo_url = f"/api/livetv/channel-logo/{tvg_id}"
            else:
                logo_url = ""
            result.append({
                "tvg_id": tvg_id,
                "name": ch["name"],
                "number": ch.get("number", ""),
                "logo_url": logo_url,
                "programs": programs,
                "readonly": True,
            })

    # ── Dynamic Stash channels (editable) ──────────────────────────────────
    if stash_enabled:
        await _ensure_stash_schedules()
        channels = _stash_channels_cache["data"]
        if channels is None:
            channels = await _get_stash_channels()

        for ch in channels:
            tvg_id = ch["tvg_id"]
            schedule = _stash_schedule.get(tvg_id, [])
            programs = []
            for entry in schedule:
                if entry["stop_ts"] <= day_start or entry["start_ts"] >= day_end:
                    continue
                programs.append({
                    "eid": entry.get("eid", ""),
                    "start_ts": entry["start_ts"],
                    "stop_ts": entry["stop_ts"],
                    "title": entry["title"],
                    "scene_id": entry.get("scene_id"),
                    "genre": entry.get("genre", ""),
                })
            custom = _custom_logo_path(tvg_id)
            if custom:
                logo_url = f"/api/livetv/channel-logo/{tvg_id}?v={int(os.path.getmtime(custom))}"
            elif ch.get("logo"):
                logo_url = f"/api/livetv/channel-logo/{tvg_id}"
            else:
                logo_url = ""
            result.append({
                "tvg_id": tvg_id,
                "name": ch["name"],
                "number": ch.get("number", ""),
                "logo_url": logo_url,
                "programs": programs,
                "stash_type": ch.get("stash_type", ""),
            })

    return JSONResponse({"date": date_label, "day_start": day_start, "day_end": day_end, "channels": result})


async def endpoint_channel_logo_get(request: Request):
    """Serve the logo for a channel: custom file → Stash proxy → 404."""
    tvg_id = request.path_params.get("tvg_id", "")

    custom = _custom_logo_path(tvg_id)
    if custom:
        mt = mimetypes.guess_type(custom)[0] or "image/jpeg"
        return FileResponse(custom, media_type=mt, headers={"Cache-Control": "public, max-age=3600"})

    channels = _stash_channels_cache["data"] or []
    if not channels:
        channels = await _get_stash_channels()
    ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if ch and ch.get("logo"):
        from api.image_routes import _proxy_image
        return await _proxy_image(ch["logo"])

    tunarr_channels = _m3u_cache.get("data") or []
    tunarr_ch = next((c for c in tunarr_channels if c["tvg_id"] == tvg_id), None)
    if tunarr_ch and tunarr_ch.get("logo"):
        from api.image_routes import _proxy_image
        return await _proxy_image(tunarr_ch["logo"])

    return Response(status_code=404)


async def endpoint_channel_logo_upload(request: Request):
    """Upload a custom logo for a channel (multipart/form-data, field name: 'file')."""
    tvg_id = request.path_params.get("tvg_id", "")
    if not re.match(r'^[a-zA-Z0-9_-]{1,60}$', tvg_id):
        return JSONResponse({"error": "invalid tvg_id"}, status_code=400)

    try:
        form = await request.form()
        upload = form.get("file")
        if not upload or not getattr(upload, "filename", None):
            return JSONResponse({"error": "no file provided"}, status_code=400)

        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            return JSONResponse({"error": "unsupported file type"}, status_code=400)

        logo_dir = _logo_dir()
        # Remove any existing custom logo for this channel (any extension)
        for existing in glob.glob(os.path.join(logo_dir, f"{tvg_id}.*")):
            os.remove(existing)

        dest = os.path.join(logo_dir, f"{tvg_id}{ext}")
        content = await upload.read()
        with open(dest, "wb") as fh:
            fh.write(content)

        logger.info(f"LiveTV: custom logo saved for '{tvg_id}' ({len(content)} bytes) → {dest}")
        return JSONResponse({"ok": True})
    except Exception as exc:
        logger.error(f"LiveTV: logo upload failed for '{tvg_id}': {exc}")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def endpoint_epg_scene_match(request: Request):
    """Search Stash for a scene matching an XMLTV title + year.

    Returns full scene metadata (same shape as endpoint_scene_detail) plus
    {"match": true} when exactly one title-exact scene is found, or
    {"match": false} when there is no confident hit.
    """
    title = request.query_params.get("title", "").strip()
    year_str = request.query_params.get("year", "").strip()

    if not title:
        return JSONResponse({"match": False})

    from core import stash_client

    scene_filter: dict = {
        "title": {"modifier": "EQUALS", "value": title}
    }
    if year_str:
        try:
            y = int(year_str)
            scene_filter["date"] = {
                "modifier": "BETWEEN",
                "value": f"{y}-01-01",
                "value2": f"{y}-12-31",
            }
        except ValueError:
            pass

    _MATCH_FIELDS = (
        "id title code date details o_counter play_count rating100 organized "
        "studio { name } performers { name } tags { name } files { duration }"
    )
    query = (
        f"query($filter: FindFilterType, $scene_filter: SceneFilterType) {{"
        f" findScenes(filter: $filter, scene_filter: $scene_filter)"
        f" {{ count scenes {{ {_MATCH_FIELDS} }} }} }}"
    )
    data = await stash_client.call_graphql(
        query, {"filter": {"per_page": 5}, "scene_filter": scene_filter}
    )
    scenes = (data or {}).get("findScenes", {}).get("scenes", [])

    if len(scenes) != 1:
        return JSONResponse({"match": False})

    scene = scenes[0]
    files = scene.get("files") or []
    duration = files[0].get("duration") if files else None

    return JSONResponse({
        "match": True,
        "id": scene.get("id"),
        "title": scene.get("title") or "",
        "code": scene.get("code") or "",
        "date": scene.get("date") or "",
        "details": scene.get("details") or "",
        "rating": scene.get("rating100"),
        "o_counter": scene.get("o_counter", 0),
        "play_count": scene.get("play_count", 0),
        "organized": scene.get("organized", False),
        "studio": (scene.get("studio") or {}).get("name", ""),
        "performers": [p["name"] for p in (scene.get("performers") or [])],
        "tags": [t["name"] for t in (scene.get("tags") or [])],
        "duration": duration,
    })


async def endpoint_scene_detail(request: Request):
    """Return scene metadata for the guide scene-detail popup (no file paths)."""
    scene_id = request.path_params.get("scene_id", "")
    if not re.match(r'^\d+$', scene_id):
        return JSONResponse({"error": "invalid scene id"}, status_code=400)

    from core import stash_client
    scene = await stash_client.get_scene(scene_id)
    if not scene:
        return JSONResponse({"error": "not found"}, status_code=404)

    duration = None
    files = scene.get("files") or []
    if files:
        duration = files[0].get("duration")

    result = {
        "id": scene.get("id"),
        "title": scene.get("title") or "",
        "code": scene.get("code") or "",
        "date": scene.get("date") or "",
        "details": scene.get("details") or "",
        "rating": scene.get("rating100"),
        "o_counter": scene.get("o_counter", 0),
        "play_count": scene.get("play_count", 0),
        "organized": scene.get("organized", False),
        "studio": (scene.get("studio") or {}).get("name", ""),
        "performers": [p["name"] for p in (scene.get("performers") or [])],
        "tags": [t["name"] for t in (scene.get("tags") or [])],
        "duration": duration,
    }
    return JSONResponse(result)


async def endpoint_scene_screenshot(request: Request):
    """Proxy the Stash screenshot for a scene so the guide modal can display it."""
    scene_id = request.path_params.get("scene_id", "")
    if not re.match(r'^\d+$', scene_id):
        return Response(status_code=400)

    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    url = f"{stash_base}/scene/{scene_id}/screenshot"
    if apikey:
        url += f"?apikey={apikey}"

    try:
        r = await _live_client.get(url)
        if r.status_code != 200:
            return Response(status_code=r.status_code)
        ct = r.headers.get("content-type", "image/jpeg")
        return Response(r.content, media_type=ct, headers={"Cache-Control": "public, max-age=3600"})
    except Exception as exc:
        logger.error(f"LiveTV: scene screenshot proxy failed for {scene_id}: {exc}")
        return Response(status_code=502)


async def endpoint_channel_logo_delete(request: Request):
    """Delete the custom logo for a channel, reverting to Stash art or default."""
    tvg_id = request.path_params.get("tvg_id", "")
    if not re.match(r'^[a-zA-Z0-9_-]{1,60}$', tvg_id):
        return JSONResponse({"error": "invalid tvg_id"}, status_code=400)

    custom = _custom_logo_path(tvg_id)
    if not custom:
        return JSONResponse({"ok": True, "removed": False})

    try:
        os.remove(custom)
        logger.info(f"LiveTV: custom logo cleared for '{tvg_id}'")
        return JSONResponse({"ok": True, "removed": True})
    except Exception as exc:
        logger.error(f"LiveTV: logo delete failed for '{tvg_id}': {exc}")
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Schedule editing endpoints
# ---------------------------------------------------------------------------

async def endpoint_channel_scenes(request: Request):
    """GET /api/livetv/channel-scenes/{tvg_id}
    Return all scenes in the channel's configured lineup so the editor can
    present a filterable scene picker.
    """
    tvg_id = request.path_params.get("tvg_id", "")
    channels = _stash_channels_cache.get("data") or []
    ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if ch is None:
        channels = await _get_stash_channels()
        ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if ch is None:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    scenes = await _fetch_scenes_for_stash_channel(ch)
    result = []
    for s in scenes:
        result.append({
            "id":            s["id"],
            "title":         s["title"],
            "duration_sec":  s["duration_sec"],
            "organized":     s.get("organized", False),
            "rating":        s.get("rating", 0),
            "o_counter":     s.get("o_counter", 0),
            "tag_count":     s.get("tag_count", 0),
            "tags":          s.get("tags", []),
            "has_description": s.get("has_description", False),
            "genre":         _scene_genre(s),
            "thumb":         _stash_screenshot_url(s["id"]),
        })
    return JSONResponse({"scenes": result})


async def endpoint_schedule_delete(request: Request):
    """DELETE /api/livetv/schedule/{tvg_id}/{eid}
    Remove one entry and shift all subsequent entries earlier by its duration.
    """
    tvg_id = request.path_params.get("tvg_id", "")
    eid    = request.path_params.get("eid", "")

    schedule = _stash_schedule.get(tvg_id)
    if not schedule:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    idx = next((i for i, e in enumerate(schedule) if e.get("eid") == eid), None)
    if idx is None:
        return JSONResponse({"error": "entry not found"}, status_code=404)

    removed  = schedule.pop(idx)
    shift    = removed["duration_sec"]
    for entry in schedule[idx:]:
        entry["start_ts"] -= shift
        entry["stop_ts"]  -= shift

    _save_schedule()
    logger.info(f"LiveTV: deleted schedule entry {eid} from '{tvg_id}', shifted {len(schedule)-idx} entries by -{shift:.1f}s")
    return JSONResponse({"ok": True})


async def endpoint_schedule_reorder(request: Request):
    """POST /api/livetv/schedule/{tvg_id}/reorder
    Body: {"eids": ["eid1", "eid2", ...]} — full ordered list for the channel.
    Rebuilds timestamps from the current window start in the new order.
    """
    tvg_id = request.path_params.get("tvg_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    eids = body.get("eids", [])
    schedule = _stash_schedule.get(tvg_id)
    if not schedule:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    entry_map = {e["eid"]: e for e in schedule}
    unknown = [eid for eid in eids if eid not in entry_map]
    if unknown:
        return JSONResponse({"error": f"unknown eids: {unknown}"}, status_code=400)

    window_start = schedule[0]["start_ts"]
    reordered    = [entry_map[eid] for eid in eids]
    # Any entries not in the submitted list go at the end (shouldn't happen in normal use)
    submitted    = set(eids)
    tail         = [e for e in schedule if e["eid"] not in submitted]
    new_schedule = reordered + tail

    cursor = window_start
    for entry in new_schedule:
        entry["start_ts"] = cursor
        entry["stop_ts"]  = cursor + entry["duration_sec"]
        cursor = entry["stop_ts"]

    _stash_schedule[tvg_id] = new_schedule
    _save_schedule()
    logger.info(f"LiveTV: reordered {len(new_schedule)} entries for '{tvg_id}'")
    return JSONResponse({"ok": True})


async def endpoint_schedule_insert(request: Request):
    """POST /api/livetv/schedule/{tvg_id}/insert
    Body: {"after_eid": "<eid or null>", "scene_id": "<stash scene id>"}
    Insert a scene immediately after after_eid (or at the start if null),
    then shift all subsequent entries forward by the scene's duration.
    """
    tvg_id = request.path_params.get("tvg_id", "")
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    after_eid = body.get("after_eid")   # None → insert at beginning
    scene_id  = body.get("scene_id", "")

    schedule = _stash_schedule.get(tvg_id, [])
    channels = _stash_channels_cache.get("data") or []
    ch = next((c for c in channels if c["tvg_id"] == tvg_id), None)
    if ch is None:
        return JSONResponse({"error": "channel not found"}, status_code=404)

    scenes = await _fetch_scenes_for_stash_channel(ch)
    scene  = next((s for s in scenes if s["id"] == scene_id), None)
    if scene is None:
        return JSONResponse({"error": "scene not found in channel lineup"}, status_code=404)

    new_entry = {
        "eid":          _new_eid(),
        "scene_id":     scene["id"],
        "title":        scene["title"],
        "duration_sec": scene["duration_sec"],
        "genre":        _scene_genre(scene),
        "rating":       scene.get("rating", 0),
        "o_counter":    scene.get("o_counter", 0),
        "start_ts":     0,
        "stop_ts":      0,
    }

    if after_eid is None:
        insert_idx = 0
    else:
        idx = next((i for i, e in enumerate(schedule) if e.get("eid") == after_eid), None)
        if idx is None:
            return JSONResponse({"error": "after_eid not found"}, status_code=404)
        insert_idx = idx + 1

    # Anchor: the start time of whatever currently occupies insert_idx (or end of schedule)
    if schedule and insert_idx < len(schedule):
        insert_start = schedule[insert_idx]["start_ts"]
    elif schedule:
        insert_start = schedule[-1]["stop_ts"]
    else:
        insert_start = time.time()

    new_entry["start_ts"] = insert_start
    new_entry["stop_ts"]  = insert_start + new_entry["duration_sec"]
    schedule.insert(insert_idx, new_entry)

    # Shift everything after the new entry forward
    shift = new_entry["duration_sec"]
    for entry in schedule[insert_idx + 1:]:
        entry["start_ts"] += shift
        entry["stop_ts"]  += shift

    _stash_schedule[tvg_id] = schedule
    _save_schedule()
    logger.info(f"LiveTV: inserted scene {scene_id} at position {insert_idx} in '{tvg_id}'")
    return JSONResponse({"ok": True, "eid": new_entry["eid"]})
