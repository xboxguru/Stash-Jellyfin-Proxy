import json
import logging
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse, RedirectResponse
from starlette.requests import Request
from starlette.background import BackgroundTask
import config
from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import decode_id, is_vertical_id

logger = logging.getLogger(__name__)

stream_client = httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False), timeout=None)

async def endpoint_playback_info(request: Request):
    raw_item_id = request.path_params.get("item_id", "")
    logger.info(f"PlaybackInfo: {request.method} item={raw_item_id!r}")

    # §2.3.1 investigation — capture the FULL raw request so we can see exactly what Wholphin
    # sends when the user picks "Play with -> Transcoding".  The transcode choice most likely
    # shows up as EnableDirectPlay/EnableDirectStream=false + EnableTranscoding=true in the
    # PlaybackInfo POST body (stock Jellyfin protocol) — flags our response currently ignores.
    # Log the query, the exact flag VALUES, the raw JSON body, and the full DeviceProfile.
    _ua = request.headers.get("user-agent", "")
    _client = next((c for c in ["Infuse", "Wholphin", "Findroid", "Fladder", "ErsatzTV", "VLC", "Jellyfin"] if c in _ua), (_ua.split("/")[0][:24] or "Unknown"))
    logger.info(f"F2 PlaybackInfo request: client={_client!r} {request.method} {request.url.path}"
                f"{('?' + request.url.query) if request.url.query else ''}")
    logger.debug(f"F2 PlaybackInfo user-agent: {_ua!r}")
    # When the client picks "Play with -> Transcoding" it sends EnableDirectPlay=false +
    # EnableTranscoding=true.  Honor it (stock Jellyfin protocol): advertise HLS-only so the
    # client actually reaches the transcode path.  Otherwise it keeps our dual-advertised
    # DirectPlay support and direct-plays, never fetching the TranscodingUrl (§2.3.1 finding).
    force_transcode_only = False
    if request.method == "POST":
        try:
            _raw = await request.body()
            _body = json.loads(_raw) if _raw else {}
            _dp = _body.get("DeviceProfile") or {}
            force_transcode_only = (_body.get("EnableDirectPlay") is False
                                    and _body.get("EnableTranscoding") is not False)
            logger.info(
                f"F2 PlaybackInfo flags: item={raw_item_id!r} "
                f"EnableDirectPlay={_body.get('EnableDirectPlay')} "
                f"EnableDirectStream={_body.get('EnableDirectStream')} "
                f"EnableTranscoding={_body.get('EnableTranscoding')} "
                f"AllowVideoStreamCopy={_body.get('AllowVideoStreamCopy')} "
                f"MaxStreamingBitrate={_body.get('MaxStreamingBitrate')} "
                f"DeviceProfile.MaxStreamingBitrate={_dp.get('MaxStreamingBitrate')} "
                f"AudioStreamIndex={_body.get('AudioStreamIndex')}"
            )
            logger.debug(f"F2 PlaybackInfo RAW body: {_raw.decode('utf-8', 'replace')}")
            logger.debug(f"F2 PlaybackInfo DeviceProfile: {json.dumps(_dp)[:4000]}")
        except Exception as e:
            logger.warning(f"F2 PlaybackInfo body parse failed: {e}")

    # Live TV channels have their own playback path
    from api import live_tv_routes, live_tv_data as _ltd
    ch = await _ltd.get_channel_by_jellyfin_id(raw_item_id)
    if ch is not None:
        if ch.get("stash_type"):
            return await live_tv_routes.stash_channel_playback_info(ch, raw_item_id, request)
        return await live_tv_routes.channel_playback_info(ch, raw_item_id, request)

    item_id = decode_id(raw_item_id)
    # If decode returned the ID unchanged or decoded to a non-scene prefix,
    # this isn't a playable library item — the channel lookup already failed.
    if item_id == raw_item_id or item_id.startswith("ch-") or item_id.startswith("channel-"):
        logger.warning(f"PlaybackInfo: unrecognized item ID {raw_item_id!r}, returning 404")
        return JSONResponse({"error": "Item not found"}, status_code=404)
    raw_id = item_id.replace("scene-", "")
    scene = await stash_client.get_scene(raw_id)

    if not scene:
        return JSONResponse({"error": "Item not found"}, status_code=404)

    # Feature 1 — a vertical-context item (played from the Vertical Multi-View library,
    # carrying a 'vscene-' id) drives the triptych compositor: select side clips,
    # advertise a session-scoped HLS transcode with direct play disabled.  A normal
    # library keeps the plain 'scene-' id and never reaches this branch.
    if is_vertical_id(raw_item_id) and getattr(config, "ENABLE_VERTICAL_MULTI", False):
        from api import vertical_routes
        return await vertical_routes.vertical_playback_info(scene, raw_id, request)

    # Feature 3 — pre-upload the funscript now (best-effort) so Handy activation on the first
    # /sessions/playing event reuses the cached URL instead of preparing it inline.
    from api import handy_controller
    handy_controller.prewarm(scene)

    # Confirmed safe (2026-07-15): normal play sends EnableDirectPlay=true, only "Play with ->
    # Transcoding" sends false — so honoring the flag disables DirectPlay solely on explicit
    # client request, leaving normal playback untouched.
    logger.info(f"F2 decision: item={raw_item_id!r} force_transcode_only={force_transcode_only}")
    jellyfin_item = jellyfin_mapper.format_jellyfin_item(scene, force_transcode_only=force_transcode_only)
    return JSONResponse({
        "MediaSources": jellyfin_item.get("MediaSources", []),
        "PlaySessionId": f"stash_{raw_id}"
    })

def _requires_transcode(scene: dict) -> bool:
    if not scene or not scene.get("files"): 
        return False
    
    file_data = scene["files"][0]
    v_codec = str(file_data.get("video_codec", "")).lower()
    container = str(file_data.get("format", "")).lower()
    
    safe_codecs = ["h264", "h265", "hevc", "avc", "vp8", "vp9", "av1"]
    safe_containers = ["mp4", "m4v", "mov", "webm"]
    
    return (v_codec and v_codec not in safe_codecs) or (container and container not in safe_containers)

async def _rewrite_hls_playlist(stash_base: str, raw_id: str, item_id: str, apikey: str) -> Response:
    stash_m3u8_url = f"{stash_base}/scene/{raw_id}/stream.m3u8"
    if apikey:
        stash_m3u8_url += f"?apikey={apikey}"
    # §2.8 — record the resolved Stash transcode URL we proxy to (apikey redacted).
    logger.info(f"HLS: proxying Stash transcode scene={raw_id} -> {stash_base}/scene/{raw_id}/stream.m3u8")
    
    try:
        m3u8_resp = await stream_client.get(stash_m3u8_url, timeout=10.0)
        if m3u8_resp.status_code == 200:
            rewritten_lines = [
                f"/Videos/{item_id}/hls/{line.split('?')[0].split('/')[-1]}" 
                if line.strip() and not line.startswith("#") else line 
                for line in m3u8_resp.text.splitlines()
            ]
            return Response(content="\n".join(rewritten_lines), media_type="application/x-mpegURL", headers={"Access-Control-Allow-Origin": "*"})
    except Exception as e:
        logger.error(f"Failed to fetch HLS playlist: {e}")
        
    return Response(status_code=500)

async def _stream_passthrough(url: str, request: Request, is_download: bool = False, download_filename: str = None) -> Response:
    headers = dict(request.headers)
    headers.pop("host", None)
    range_header = headers.get("range") or headers.get("Range")
    
    logger.debug(f"Stream request initiated -> URL: {url} | Range: {range_header}")

    try:
        req = stream_client.build_request(request.method, url, headers=headers)
        r = await stream_client.send(req, stream=True)

        resp_headers = dict(r.headers)
        for h in ["content-encoding", "transfer-encoding", "connection"]:
            resp_headers.pop(h, None)
        
        if range_header and r.status_code == 206 and "content-range" not in resp_headers:
            logger.warning(f"Stash returned 206 but missing Content-Range for URL: {url}")
            
        if is_download and download_filename:
            resp_headers["Content-Disposition"] = f'attachment; filename="{download_filename}"'
        
        if request.method == "HEAD":
            await r.aclose()
            return Response(status_code=r.status_code, headers=resp_headers)

        async def stream_generator():
            async for chunk in r.aiter_bytes(chunk_size=8192): 
                yield chunk
            
        async def cleanup():
            await r.aclose()
            logger.debug(f"Stream closed -> URL: {url}")

        return StreamingResponse(stream_generator(), status_code=r.status_code, headers=resp_headers, background=BackgroundTask(cleanup))

    except Exception as e:
        logger.error(f"Stream passthrough failed: {e}")
        return Response(status_code=500)

async def endpoint_subtitle(request: Request):
    item_id = decode_id(request.path_params.get("item_id", ""))
    if not item_id.startswith("scene-"):
        return Response(status_code=404)

    raw_id = item_id.replace("scene-", "")
    stream_index = int(request.path_params.get("stream_index", 2))

    scene = await stash_client.get_scene(raw_id)
    if not scene:
        return Response(status_code=404)

    captions = scene.get("captions") or []
    caption_base = (scene.get("paths") or {}).get("caption")
    cap_index = stream_index - 2  # video=0, audio=1, subtitles start at 2

    if not caption_base or cap_index < 0 or cap_index >= len(captions):
        return Response(status_code=404)

    cap = captions[cap_index]
    lang = cap.get("language_code") or "und"
    cap_type = (cap.get("caption_type") or "srt").lower()

    url = f"{caption_base}?lang={lang}&type={cap_type}"
    apikey = getattr(config, "STASH_API_KEY", "")
    if apikey:
        url += f"&apikey={apikey}"

    try:
        r = await stream_client.get(url)
        if r.status_code == 200:
            logger.debug(f"Subtitle proxied: scene={raw_id} lang={lang} type={cap_type}")
            return Response(content=r.content, media_type="text/plain; charset=utf-8",
                            headers={"Cache-Control": "public, max-age=3600"})
        logger.warning(f"Stash returned {r.status_code} for subtitle {url}")
    except Exception as e:
        logger.error(f"Subtitle proxy failed: {e}")

    return Response(status_code=404)

async def endpoint_stream(request: Request):
    raw_item_id = request.path_params.get("item_id", "")

    # Live TV channels: clients may build a standard /Videos/{id}/stream or
    # /master.m3u8 transcode URL from the channel ID instead of using the
    # PlaybackInfo Path.  Redirect to the channel's live stream rather than
    # falling through to get_scene() with a channel ID.
    from api import live_tv_data as _ltd
    ch = await _ltd.get_channel_by_jellyfin_id(raw_item_id)
    if ch is not None:
        cid = raw_item_id.replace("-", "")
        if ch.get("stash_type"):
            target = f"/livetv/channels/{cid}/stash-stream.m3u8"
        else:
            target = f"/livetv/channels/{cid}/stream.m3u8"
        logger.info(
            f"Stream: Live TV channel {cid} ({ch.get('name')!r}) "
            f"req={request.url.path}?{request.url.query} → redirect {target}"
        )
        return RedirectResponse(url=target, status_code=302)

    # Feature 1 — a vertical-context id built straight into a /Videos/{id}/stream or
    # master.m3u8 URL (client bypassing PlaybackInfo) is redirected to a fresh
    # composite session, mirroring the Live TV guard above.
    if is_vertical_id(raw_item_id) and getattr(config, "ENABLE_VERTICAL_MULTI", False):
        from api import vertical_routes
        return await vertical_routes.redirect_to_composite(raw_item_id, request)

    item_id = decode_id(raw_item_id)
    raw_id = item_id.replace("scene-", "")
    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    stash_stream_url = f"{stash_base}/scene/{raw_id}/stream"
    
    scene = await stash_client.get_scene(raw_id)
    download_ext = scene["files"][0].get("format", "mp4").lower() if scene and scene.get("files") else "mp4"
    is_download = "download" in request.url.path.lower()
    
    if is_download and scene and scene.get("files"):
        dl_url = f"{stash_base}/scene/{raw_id}/stream"
        if apikey: 
            dl_url += f"?apikey={apikey}&download=true"
        logger.info(f"Redirecting client download to raw file: {raw_id}")
        return RedirectResponse(url=dl_url, status_code=302)
        
    # Feature 2 — Client-Forced Transcoding.  Route by URL intent, not codec: any request
    # whose path ends in .m3u8 is served as Stash HLS.  A genuinely incompatible file always
    # takes this path; a compatible file only does so when the forced-transcode kill-switch is
    # on (client picked "Play with -> Transcoding").  Seek is client-native — the full VOD
    # playlist is served unchanged, no start= threading here (§2.2).
    path_is_m3u8 = request.url.path.lower().endswith(".m3u8")
    forced_enabled = getattr(config, "ENABLE_FORCED_TRANSCODE", True)
    needs_transcode = _requires_transcode(scene)

    if path_is_m3u8:
        # §2.3.1 investigation — dump EVERY query param the client appended to the bare
        # TranscodingUrl, so we can see whether MaxStreamingBitrate/maxWidth/maxHeight/
        # videoBitRate ever arrive here (vs. only in the PlaybackInfo POST body above).
        logger.info(
            f"F2 transcode request: scene={raw_id} path={request.url.path} "
            f"query_params={dict(request.query_params)}"
        )

    if path_is_m3u8 and (needs_transcode or forced_enabled):
        logger.info(
            f"Stream: HLS transcode scene={raw_id} client_url={request.url.path}"
            f"{('?' + request.url.query) if request.url.query else ''} "
            f"needs_transcode={needs_transcode} forced_enabled={forced_enabled}"
        )
        return await _rewrite_hls_playlist(stash_base, raw_id, item_id, apikey)

    if needs_transcode:
        # Incompatible file requested without an .m3u8 extension — redirect the strict client
        # to the explicit master.m3u8 so it fetches HLS instead of raw (broken) passthrough.
        logger.debug(f"Redirecting strict client to explicit .m3u8 URL for scene {raw_id}")
        new_url = f"/Videos/{item_id}/master.m3u8"
        if request.url.query:
            new_url += f"?{request.url.query}"
        return RedirectResponse(url=new_url, status_code=302)

    start_ticks = next((v for k, v in request.query_params.items() if k.lower() == "starttimeticks"), None)
    if start_ticks:
        try:
            start_sec = float(start_ticks) / 10000000.0
            stash_stream_url += f"{'&' if '?' in stash_stream_url else '?'}start={start_sec}"
            # Feature 3 — hand the exact start position to the Handy controller for an instant,
            # correctly-positioned first play (best-effort; never affects streaming).
            try:
                from api import handy_controller
                handy_controller.note_start_position(raw_id, start_sec)
            except Exception:
                pass
        except ValueError:
            pass

    if apikey and "apikey=" not in stash_stream_url.lower():
        stash_stream_url += f"{'&' if '?' in stash_stream_url else '?'}apikey={apikey}"

    return await _stream_passthrough(
        url=stash_stream_url, 
        request=request, 
        is_download=is_download, 
        download_filename=f"{raw_id}.{download_ext}"
    )
    
async def endpoint_hls_segment(request: Request):
    raw_item_id = request.path_params.get("item_id", "")

    # Live TV channels serve their HLS segments from the live TV endpoints, not
    # here.  Guard so a channel ID never reaches get_scene() / Stash.
    from api import live_tv_data as _ltd
    if await _ltd.get_channel_by_jellyfin_id(raw_item_id) is not None:
        logger.warning(f"HLS segment requested for Live TV channel {raw_item_id!r} — not served here")
        return Response(status_code=404)

    item_id = decode_id(raw_item_id)
    raw_id = item_id.replace("scene-", "")
    segment = request.path_params.get("segment", "")
    
    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    stash_segment_url = f"{stash_base}/scene/{raw_id}/stream.m3u8/{segment}"
    if apikey: 
        stash_segment_url += f"?apikey={apikey}"
        
    return await _stream_passthrough(stash_segment_url, request)