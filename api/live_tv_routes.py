import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import config
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from core.jellyfin_mapper import encode_id, decode_id
from api.live_tv_engine import _ffmpeg_manager
from api.live_tv_data import (
    _live_client, _stash_channels_cache, _stash_schedule, _stash_program_map,
    _channel_stream_map, _program_info_map, _stash_channel_map,
    _get_stash_channels, _get_channels, _get_programs,
    _ensure_stash_schedules, _live_tv_enabled, _stash_screenshot_url,
    _channel_to_jellyfin, _program_to_jellyfin, _current_program_for,
    get_channel_by_jellyfin_id, get_program_by_jellyfin_id,
    _is_stash_item, _normalize_id, _build_stash_channel_playlist,
    _get_stash_programs_for_channel, _upcoming_scheduled_segments,
)

logger = logging.getLogger(__name__)


async def _all_channels() -> tuple[list[dict], list[dict]]:
    """Return (tunarr_channels, stash_channels) gated by config flags."""
    tunarr = await _get_channels() if getattr(config, "ENABLE_TUNARR", False) else []
    stash = await _get_stash_channels() if getattr(config, "ENABLE_STASH_CHANNELS", False) else []
    return tunarr, stash


async def stash_channel_playback_info(ch: dict, item_id: str, request=None) -> JSONResponse:
    """PlaybackInfo for a dynamic Stash channel.

    Advertises an HLS TranscodingUrl and disables direct play so the client uses
    hls.js / ExoPlayer HlsMediaSource.  Pre-warms FFmpeg so segments are ready
    by the time the client fetches the manifest.
    """
    item_id = item_id.replace("-", "")
    play_session_id = f"stash_live_{ch['tvg_id']}"

    # Relative HLS transcoding URL.  Advertising a TranscodingUrl with
    # TranscodingSubProtocol=hls makes jellyfin-web use hls.js and Jellyfin
    # Android TV use ExoPlayer's HlsMediaSource, both pointed straight at our
    # .m3u8.  Direct play (static=true) must be disabled — otherwise the
    # client requests /Videos/{id}/stream expecting a raw byte stream and
    # chokes on the HLS playlist it gets instead.
    transcode_url = f"/livetv/channels/{item_id}/stash-stream.m3u8"

    # Pre-warm FFmpeg so the manifest has segments by the time the client
    # fetches it (the readiness gate waits for >=3 segments).
    await _ensure_stash_schedules()
    playlist_result = await _build_stash_channel_playlist(ch)
    if playlist_result is not None:
        _entries, seek = playlist_result
        await _ffmpeg_manager.ensure(item_id, ch, seek)

    source: dict = {
        "Protocol": "Http",
        "Id": item_id,
        "Path": transcode_url,
        "Type": "Default",
        "Name": ch.get("name", "Live"),
        "IsRemote": False,
        "ReadAtNativeFramerate": True,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsTranscoding": True,
        "SupportsDirectStream": False,
        "SupportsDirectPlay": False,
        "IsInfiniteStream": True,
        "IsLive": True,
        "UseMostCompatibleTranscodingProfile": True,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "RequiresLooping": False,
        "SupportsProbing": False,
        "TranscodingUrl": transcode_url,
        "TranscodingSubProtocol": "hls",
        "TranscodingContainer": "ts",
        "MediaStreams": [
            {"VideoRange": "SDR", "VideoRangeType": "SDR", "AudioSpatialFormat": "None",
             "DisplayTitle": "SDR", "IsInterlaced": False, "IsDefault": False,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Video", "Index": -1,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
            {"VideoRange": "Unknown", "VideoRangeType": "Unknown", "AudioSpatialFormat": "None",
             "DisplayTitle": "", "IsInterlaced": False, "IsDefault": False,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Audio", "Index": -1,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
        ],
        "MediaAttachments": [],
        "Formats": [],
        "RequiredHttpHeaders": {},
        "DefaultAudioStreamIndex": -1,
        "HasSegments": False,
    }

    logger.info(f"LiveTV: stash channel playback_info '{ch['name']}' ({item_id}) → {transcode_url}")
    return JSONResponse({
        "MediaSources": [source],
        "PlaySessionId": play_session_id,
    })


async def endpoint_stash_channel_stream(request: Request):
    """FFmpeg-based live HLS stream for a Stash channel.

    On first play request, spawns an FFmpeg process that:
      • opens each scene as its own input via raw Stash HTTP streams (no
        Stash-side transcode — byte-range seeking via -ss on the first input)
      • uses the concat filter (not demuxer) with per-input normalization to
        feed the encoder a uniform 1920x1080 yuv420p 30fps / 48 kHz stereo
        stream so no decoder/filter-graph reconfigure happens at scene
        transitions
      • transcodes once to H.264+AAC
      • writes live HLS segments to a per-channel temp directory

    The process is killed automatically after LIVE_TV_IDLE_TIMEOUT seconds
    (default 300 s) of no manifest/segment requests.  Restarted on next play.

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
    is_manifest = request.url.path.lower().endswith(".m3u8")

    ch = _stash_channel_map.get(channel_id) or _stash_channel_map.get(channel_id_clean)
    if not ch:
        await _get_stash_channels()
        ch = _stash_channel_map.get(channel_id) or _stash_channel_map.get(channel_id_clean)
    if not ch:
        logger.warning(f"LiveTV: stash-stream — unknown channel {channel_id}")
        return Response(status_code=404)

    if is_manifest:
        logger.debug(f"LiveTV: stash-stream manifest requested for '{ch.get('name')}' ({channel_id_clean})")

    await _ensure_stash_schedules()

    playlist_result = await _build_stash_channel_playlist(ch)
    if playlist_result is None:
        logger.warning(f"LiveTV: stash-stream — no current program for {ch['tvg_id']}")
        return Response(status_code=404)
    _entries, seek = playlist_result

    ok = await _ffmpeg_manager.ensure(channel_id_clean, ch, seek)
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
    # Strip #EXT-X-DISCONTINUITY: all content is normalized to the same
    # codec/resolution/fps so there's no actual discontinuity.  The tag causes
    # Android TV's hardware H.264 decoder to tear down and reinitialize (~20s
    # freeze per scene transition) even though the codec parameters are identical.
    # We no longer inject #EXT-X-SERVER-CONTROL:HOLD-BACK — real Jellyfin/Tunarr
    # do not use it.  #EXT-X-PROGRAM-DATE-TIME (added via the FFmpeg
    # program_date_time hls_flag) is the correct live-edge anchor and replaces
    # that hack.
    base = f"{request.url.scheme}://{request.url.netloc}"
    out_lines = []
    for line in raw.splitlines():
        s = line.strip()
        if s == "#EXT-X-DISCONTINUITY":
            continue
        if s and not s.startswith("#"):
            out_lines.append(f"{base}/livetv/channels/{channel_id_clean}/seg/{s}")
        else:
            out_lines.append(line)

    logger.trace(
        f"LiveTV FFmpeg: served manifest for '{ch['name']}' "
        f"channel={channel_id_clean} seek={seek:.1f}s entries={len(_entries)}"
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

    if not re.match(r"^seg\d+\.ts$", seg_name):
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

    return StreamingResponse(
        _iter(),
        media_type="video/mp2t",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# Public lookup API (used by metadata_routes and stream_routes)
# ---------------------------------------------------------------------------


async def channel_playback_info(ch: dict, item_id: str, request=None) -> JSONResponse:
    """PlaybackInfo for a Tunarr TvChannel — redirects directly to Tunarr HLS, no local transcoding."""
    item_id = item_id.replace("-", "")
    play_session_id = f"live_{item_id}"

    transcode_url = f"/livetv/channels/{item_id}/tunarr-relay.m3u8"
    logger.info(f"LiveTV: channel_playback_info for {ch.get('name')} ({item_id}) → {transcode_url}")

    source: dict = {
        "Protocol": "Http",
        "Id": item_id,
        "Path": transcode_url,
        "Type": "Default",
        "Name": ch.get("name", "Live"),
        "IsRemote": False,
        "ReadAtNativeFramerate": True,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "SupportsTranscoding": True,
        "SupportsDirectStream": False,
        "SupportsDirectPlay": False,
        "IsInfiniteStream": True,
        "IsLive": True,
        "UseMostCompatibleTranscodingProfile": True,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "RequiresLooping": False,
        "SupportsProbing": False,
        "TranscodingUrl": transcode_url,
        "TranscodingSubProtocol": "hls",
        "TranscodingContainer": "ts",
        "MediaStreams": [
            {"VideoRange": "SDR", "VideoRangeType": "SDR", "AudioSpatialFormat": "None",
             "DisplayTitle": "SDR", "IsInterlaced": False, "IsDefault": False,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Video", "Index": -1,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
            {"VideoRange": "Unknown", "VideoRangeType": "Unknown", "AudioSpatialFormat": "None",
             "DisplayTitle": "", "IsInterlaced": False, "IsDefault": False,
             "IsForced": False, "IsHearingImpaired": False, "Type": "Audio", "Index": -1,
             "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False},
        ],
        "MediaAttachments": [],
        "Formats": [],
        "RequiredHttpHeaders": {},
        "DefaultAudioStreamIndex": -1,
        "HasSegments": False,
    }

    return JSONResponse({
        "MediaSources": [source],
        "PlaySessionId": play_session_id,
    })


async def endpoint_tunarr_relay_stream(request: Request):
    """Redirect to Tunarr's stream init URL — triggers Tunarr's FFmpeg and returns the HLS multi-variant playlist."""
    channel_id = request.path_params.get("channel_id", "").replace("-", "")

    stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        await _get_channels()
        stream_url = _channel_stream_map.get(channel_id)
    if not stream_url:
        logger.warning(f"LiveTV relay: unknown channel {channel_id}")
        return Response(status_code=404)

    logger.info(f"LiveTV relay: redirecting {channel_id} → {stream_url}")
    return RedirectResponse(url=stream_url, status_code=302)


async def endpoint_live_streams_open(request: Request):
    """POST /LiveStreams/Open — stub.

    PlaybackInfo uses RequiresOpening=False so no client calls this in normal
    flow.  Registered to prevent 404s from clients with stale cached state.
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    open_token = (data.get("OpenToken") or request.query_params.get("OpenToken", "")).strip()
    logger.info(f"LiveTV: POST /LiveStreams/Open (unexpected) token={open_token!r}")
    return Response(status_code=204)


async def endpoint_live_streams_close(request: Request):
    try:
        data = await request.json()
    except Exception:
        data = {}
    live_stream_id = data.get("LiveStreamId") or request.query_params.get("LiveStreamId", "")
    logger.info(f"LiveTV: POST /LiveStreams/Close id={live_stream_id!r}")
    return Response(status_code=204)


async def endpoint_live_streams_ping(request: Request):
    live_stream_id = request.query_params.get("LiveStreamId", "")
    logger.debug(f"LiveTV: POST /LiveStreams/Ping id={live_stream_id!r}")
    return Response(status_code=204)


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
            logger.debug(f"LiveTV: Tunarr redirected {stream_url} -> {final_url}")
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

        logger.debug(f"LiveTV: proxied m3u8 for channel {channel_id}")
        return Response(
            content="\n".join(lines),
            media_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache, no-store"},
        )
    except Exception as e:
        logger.error(f"LiveTV: m3u8 proxy failed for channel {channel_id}: {e}")
        return Response(status_code=500)


async def endpoint_program_detail(request: Request):
    program_id = request.path_params.get("program_id", "")
    logger.debug(f"LiveTV: GET /livetv/programs/{program_id}")
    prog = await get_program_by_jellyfin_id(program_id)
    if prog is None:
        return Response(status_code=404)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    tunarr_channels, stash_channels = await _all_channels()
    channels_by_tvg_id = {ch["tvg_id"]: ch for ch in tunarr_channels + stash_channels}
    return JSONResponse(_program_to_jellyfin(prog, server_id, channels_by_tvg_id, program_id))


async def endpoint_timer_defaults(request: Request):
    logger.debug("LiveTV: GET /livetv/timers/defaults")
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
    logger.debug("LiveTV: GET /livetv/recordings/folders")
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})


async def endpoint_live_tv_info(request: Request):
    logger.debug("LiveTV: GET /livetv/info")
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
    logger.debug(f"LiveTV: GET /livetv/channels params={dict(request.query_params)}")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    tunarr_channels, stash_channels = await _all_channels()
    all_channels = tunarr_channels + stash_channels
    logger.debug(f"LiveTV: returning {len(all_channels)} channels ({len(tunarr_channels)} Tunarr, {len(stash_channels)} Stash)")

    add_current = request.query_params.get("addCurrentProgram", "").lower() == "true"
    tunarr_programs: list[dict] = []
    channels_by_tvg_id: dict = {ch["tvg_id"]: ch for ch in all_channels}
    if add_current and tunarr_channels:
        tunarr_programs = await _get_programs()

    items = []
    for ch in all_channels:
        eid = encode_id("ch", ch["tvg_id"])
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
                    prog_id = encode_id("program", f"{ch['tvg_id']}|{raw['start']}").replace("-", "")
                    _stash_program_map[prog_id] = raw
                    logger.debug(f"LiveTV: addCurrentProgram stored key={prog_id} for ch={ch['tvg_id']} start={raw['start']}")
                    current = _program_to_jellyfin(raw, server_id, channels_by_tvg_id)
            else:
                current = _current_program_for(ch["tvg_id"], tunarr_programs, server_id, channels_by_tvg_id)
        items.append(_channel_to_jellyfin(ch, server_id, eid, current))

    return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})


async def endpoint_channel_single(request: Request):
    """Return a single TvChannel object by encoded ID.

    Jellyfin Android calls GET /LiveTv/Channels/{Id} for individual channel
    lookups.  Without this route the request falls to the blackhole which
    returns Items:[] — Jellyfin Android throws InvalidContentException and
    crashes the guide view.
    """
    channel_id = request.path_params.get("channel_id", "").replace("-", "")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    tunarr_channels = await _get_channels() if getattr(config, "ENABLE_TUNARR", False) else []
    stash_channels = await _get_stash_channels() if getattr(config, "ENABLE_STASH_CHANNELS", False) else []

    for ch in tunarr_channels + stash_channels:
        eid = encode_id("ch", ch["tvg_id"]).replace("-", "")
        if eid == channel_id:
            return JSONResponse(_channel_to_jellyfin(ch, server_id, eid))

    return Response(status_code=404)


async def endpoint_channel_now_playing(request: Request):
    """Return the currently playing scene for an active Stash channel's FFmpeg stream.

    Query params:
        tvg_id: channel tvg_id (e.g. "shorts")
    """
    tvg_id = request.query_params.get("tvg_id", "")
    if not tvg_id:
        return JSONResponse({"active": False, "error": "tvg_id required"}, status_code=400)

    enc = encode_id("ch", tvg_id).replace("-", "")
    if not _ffmpeg_manager.is_alive(enc):
        return JSONResponse({"active": False})

    stash_channels = await _get_stash_channels()
    ch = next((c for c in stash_channels if c["tvg_id"] == tvg_id), None)
    scene_info = _ffmpeg_manager.get_scene_at(enc, ch)
    if not scene_info:
        return JSONResponse({"active": True})

    return JSONResponse({"active": True, **scene_info})


async def endpoint_shorts_block_preview(request: Request):
    """Return the scene list for a Shorts EPG block, read from the stored schedule.

    Query params:
        ts:     block start Unix timestamp (matched against stored block start_ts)
        tvg_id: (optional) channel tvg_id; defaults to first shorts channel found
    """
    ts_str = request.query_params.get("ts", "")
    tvg_id = request.query_params.get("tvg_id", "")
    try:
        block_start = float(ts_str)
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid ts"}, status_code=400)

    if not tvg_id:
        stash_channels = await _get_stash_channels()
        sch = next((c for c in stash_channels if c.get("stash_type") == "shorts"), None)
        if sch:
            tvg_id = sch["tvg_id"]

    schedule = _stash_schedule.get(tvg_id, []) if tvg_id else []
    block = next((b for b in schedule if abs(b.get("start_ts", 0) - block_start) < 5), None)
    if not block:
        return JSONResponse({"scenes": []})

    scenes = [{
        "id":           seg["scene_id"],
        "title":        seg.get("title", ""),
        "start_ts":     seg["start_ts"],
        "stop_ts":      seg["stop_ts"],
        "duration_sec": seg["duration_sec"],
    } for seg in (block.get("segments") or [])]

    return JSONResponse({
        "block_start": block["start_ts"],
        "block_end":   block["stop_ts"],
        "scenes":      scenes,
    })


async def endpoint_programs(request: Request):
    logger.debug(f"LiveTV: {request.method} /livetv/programs params={dict(request.query_params)}")
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    tunarr_channels, stash_channels = await _all_channels()
    tunarr_programs = await _get_programs() if getattr(config, "ENABLE_TUNARR", False) else []

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
                "genre": entry.get("genre", ""), "desc": "", "scene_id": entry.get("scene_id"),
                "icon": _stash_screenshot_url(entry["scene_id"]) if entry.get("scene_id") else "",
            }
            prog_id = encode_id("program", f"{tvg_id}|{raw_prog['start']}")
            pid_norm = prog_id.replace("-", "")
            # Register for single-item lookup in both maps.
            # _stash_program_map is never cleared by XMLTV refreshes so the entry
            # survives concurrent/subsequent _get_programs() calls.
            _program_info_map[pid_norm] = raw_prog
            _stash_program_map[pid_norm] = raw_prog
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

    # Channel filter — ChannelIds may be:
    #   • repeated query params:  ?channelIds=a&channelIds=b  (Jellyfin Android TV)
    #   • a single comma-sep value: ?channelIds=a,b
    #   • a JSON array in a POST body (Wholphin)
    # Starlette's query_params.items() deduplicates keys (last value wins), so we
    # use multi_items() to capture every occurrence of channelIds.
    requested: set[str] = set()
    all_qs_channel_ids = [v for k, v in request.query_params.multi_items() if k.lower() == "channelids"]
    if all_qs_channel_ids:
        for val in all_qs_channel_ids:
            requested.update(val.split(","))
    else:
        body_ids = body.get("ChannelIds", body.get("channelIds", body.get("channelids")))
        if isinstance(body_ids, list):
            requested = set(str(x) for x in body_ids)
        elif isinstance(body_ids, str) and body_ids:
            requested = set(body_ids.split(","))
    # Normalize to unhyphenated hex so hyphenated UUID IDs from clients still match.
    # Drop sentinel values that clients send when they mean "no filter" (e.g. "null").
    _SENTINEL_IDS = {"null", "undefined", "", "0"}
    requested = {r.replace("-", "") for r in requested if r.lower() not in _SENTINEL_IDS}
    logger.debug(f"LiveTV: programs channel filter requested={requested or 'ALL'}")
    if requested:
        wanted = {tvg for tvg in channels_by_tvg_id
                  if encode_id("ch", tvg).replace("-", "") in requested}
        logger.debug(f"LiveTV: programs channel filter matched tvg_ids={wanted}")
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

    # Guide time-window capping strategy:
    # - If no date range provided (Wholphin home-screen widgets, old clients): default to a
    #   14h window (2h past → 12h future) so we don't flood with thousands of short-clip entries.
    # - If an explicit MaxStartDate is provided (Jellyfin Web guide, Wholphin EPG): honour it
    #   so the full guide day renders. Cap at 48h absolute max to prevent absurdly large responses.
    DEFAULT_FORWARD   = 43_200    # 12h — used only when client sends no MaxStartDate
    GUIDE_FORWARD_MAX = 172_800   # 48h — hard ceiling even when client provides a date
    GUIDE_PAST_CAP    =  7_200    #  2h back

    if max_start_ts is None:
        max_start_ts = now_ts + DEFAULT_FORWARD
    elif max_start_ts > now_ts + GUIDE_FORWARD_MAX:
        max_start_ts = now_ts + GUIDE_FORWARD_MAX
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

    # Genre filters — used by Jellyfin/Wholphin home-screen carousels
    _GENRE_PARAM_MAP = {
        "IsMovie":       ("true",  "Movie"),
        "IsSports":      ("true",  "Sports"),
        "IsKids":        ("true",  "Kids"),
        "IsNews":        ("true",  "News"),
        "IsSeries":      ("true",  None),   # "Series" means non-Movie in Jellyfin
        "IsMovie_false": ("false", "Movie"),
    }
    is_movie = _qp("IsMovie",  "").lower()
    is_sports = _qp("IsSports", "").lower()
    is_kids   = _qp("IsKids",   "").lower()
    is_news   = _qp("IsNews",   "").lower()
    is_series = _qp("IsSeries", "").lower()
    if is_movie == "true":
        programs = [p for p in programs if p.get("genre") == "Movie"]
    elif is_movie == "false":
        programs = [p for p in programs if p.get("genre") != "Movie"]
    if is_sports == "true":
        programs = [p for p in programs if p.get("genre") == "Sports"]
    if is_kids == "true":
        programs = [p for p in programs if p.get("genre") == "Kids"]
    if is_news == "true":
        programs = [p for p in programs if p.get("genre") == "News"]
    if is_series == "true":
        programs = [p for p in programs if p.get("genre") not in ("Movie", "")]

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
    logger.debug(f"LiveTV: programs returning {len(items)}/{total} items")
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


