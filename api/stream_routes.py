import logging
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse, RedirectResponse
from starlette.requests import Request
from starlette.background import BackgroundTask
import config
from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import decode_id

logger = logging.getLogger(__name__)

stream_client = httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False), timeout=None)

async def endpoint_playback_info(request: Request):
    item_id = decode_id(request.path_params.get("item_id", ""))
    raw_id = item_id.replace("scene-", "")
    scene = await stash_client.get_scene(raw_id)
    
    if not scene:
        return JSONResponse({"error": "Item not found"}, status_code=404)
        
    jellyfin_item = jellyfin_mapper.format_jellyfin_item(scene)
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
    logger.debug(f"Serving rewritten HLS Playlist for scene {raw_id}")
    stash_m3u8_url = f"{stash_base}/scene/{raw_id}/stream.m3u8"
    if apikey: 
        stash_m3u8_url += f"?apikey={apikey}"
    
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
    item_id = decode_id(request.path_params.get("item_id", ""))
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
        
    if _requires_transcode(scene):
        if not request.url.path.lower().endswith(".m3u8"):
            logger.debug(f"Redirecting strict client to explicit .m3u8 URL for scene {raw_id}")
            new_url = f"/Videos/{item_id}/master.m3u8"
            if request.url.query: 
                new_url += f"?{request.url.query}"
            return RedirectResponse(url=new_url, status_code=302)
        
        return await _rewrite_hls_playlist(stash_base, raw_id, item_id, apikey)

    start_ticks = next((v for k, v in request.query_params.items() if k.lower() == "starttimeticks"), None)
    if start_ticks:
        try:
            start_sec = float(start_ticks) / 10000000.0
            stash_stream_url += f"{'&' if '?' in stash_stream_url else '?'}start={start_sec}"
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
    item_id = decode_id(request.path_params.get("item_id", ""))
    raw_id = item_id.replace("scene-", "")
    segment = request.path_params.get("segment", "")
    
    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    stash_segment_url = f"{stash_base}/scene/{raw_id}/stream.m3u8/{segment}"
    if apikey: 
        stash_segment_url += f"?apikey={apikey}"
        
    return await _stream_passthrough(stash_segment_url, request)