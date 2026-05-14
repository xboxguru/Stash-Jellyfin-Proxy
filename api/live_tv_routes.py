import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

import config
from core.jellyfin_mapper import encode_id

logger = logging.getLogger(__name__)

_live_client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
CACHE_TTL = 300  # 5 minutes

_m3u_cache: dict = {"data": None, "ts": 0.0}
_xmltv_cache: dict = {"data": None, "ts": 0.0}

_channel_stream_map: dict[str, str] = {}   # encoded_id -> stream_url
_channel_info_map: dict[str, dict] = {}    # encoded_id -> raw channel dict
_program_info_map: dict[str, dict] = {}    # encoded_id -> raw program dict


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
        _channel_info_map.clear()
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
    ch = _channel_info_map.get(item_id) or _channel_info_map.get(normalized)
    if not ch:
        logger.warning(f"LiveTV: channel lookup MISS for {item_id} (map has {len(_channel_info_map)} entries)")
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
        logger.warning(f"LiveTV: program lookup MISS for {item_id} (map has {len(_program_info_map)} entries)")
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
    channels = await _get_channels()
    channels_by_tvg_id = {ch["tvg_id"]: ch for ch in channels}
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
    return JSONResponse({
        "Services": [
            {
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
            }
        ],
        "IsEnabled": bool(m3u_url),
        "HasRecordingSupport": False,
        "EnabledUsers": [],
    })


async def endpoint_channels(request: Request):
    logger.info(f"LiveTV: GET /livetv/channels params={dict(request.query_params)}")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    channels = await _get_channels()
    logger.info(f"LiveTV: returning {len(channels)} channels")

    add_current = request.query_params.get("addCurrentProgram", "").lower() == "true"
    programs: list[dict] = []
    channels_by_tvg_id: dict = {}
    if add_current:
        programs = await _get_programs()
        channels_by_tvg_id = {ch["tvg_id"]: ch for ch in channels}

    items = []
    for ch in channels:
        eid = encode_id("channel", ch["tvg_id"])
        current = None
        if add_current:
            current = _current_program_for(ch["tvg_id"], programs, server_id, channels_by_tvg_id)
        items.append(_channel_to_jellyfin(ch, server_id, eid, current))

    return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})


async def endpoint_programs(request: Request):
    logger.info(f"LiveTV: {request.method} /livetv/programs params={dict(request.query_params)}")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    channels, programs = await _get_channels(), await _get_programs()
    channels_by_tvg_id = {ch["tvg_id"]: ch for ch in channels}

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
