import logging
import time
import asyncio
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse, RedirectResponse
from starlette.requests import Request
from starlette.background import BackgroundTask, BackgroundTasks
import config
from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import decode_id
import state

logger = logging.getLogger(__name__)

async def endpoint_playback_info(request: Request):
    """Provides playback info using the robust metadata already built by the mapper."""
    item_id = decode_id(request.path_params.get("item_id", ""))
    raw_id = item_id.replace("scene-", "")
    scene = stash_client.get_scene(raw_id)
    if not scene:
        return JSONResponse({"error": "Item not found"}, status_code=404)
        
    jellyfin_item = jellyfin_mapper.format_jellyfin_item(scene)
    return JSONResponse({
        "MediaSources": jellyfin_item.get("MediaSources", []),
        "PlaySessionId": f"stash_{raw_id}"
    })

async def endpoint_sessions_playing(request: Request):
    """Receives playback start and progress reports from Jellyfin clients."""
    try:
        data = await request.json()
        session_id = data.get("PlaySessionId") or data.get("SessionId") or "unknown_session"
        item_id = decode_id(data.get("ItemId") or data.get("Item", {}).get("Id", ""))
        
        playback_ticks = float(data.get("PlaybackPositionTicks") or data.get("PositionTicks") or 0)
        runtime_ticks = float(data.get("RunTimeTicks") or data.get("Item", {}).get("RunTimeTicks") or 0)
        title = data.get("Item", {}).get("Name")
        performer = "Unknown"
        
        # --- 1. EXTRACT CLIENT METADATA ---
        client_ip = request.client.host if request.client else "Unknown"
        user_agent = request.headers.get("user-agent", "")
        
        # Respect Reverse Proxies (Docker / Nginx)
        x_forwarded = request.headers.get("x-forwarded-for")
        if x_forwarded:
            client_ip = x_forwarded.split(",")[0].strip()
        elif request.headers.get("x-real-ip"):
            client_ip = request.headers.get("x-real-ip")
            
        # Parse Client Type from User-Agent
        if "Infuse" in user_agent: client_type = "Infuse"
        elif "Wholphin" in user_agent: client_type = "Wholphin"
        elif "Findroid" in user_agent: client_type = "Findroid"
        elif "Fladder" in user_agent: client_type = "Fladder"
        elif "ErsatzTV" in user_agent: client_type = "ErsatzTV"
        elif "VLC" in user_agent: client_type = "VLC"
        elif "Jellyfin" in user_agent: client_type = "Jellyfin"
        else: client_type = user_agent.split("/")[0][:20] if user_agent else "Unknown"
        
        # Try to get User from payload, fallback to Config
        user = data.get("UserId") or getattr(config, "SJS_USER", "Admin")
        if not user: user = "Admin"
        # ----------------------------------
        
        # Fetch missing metadata directly from Stash
        if item_id.startswith("scene-"):
            raw_id = item_id.replace("scene-", "")
            scene = stash_client.get_scene(raw_id)
            if scene:
                if not title or title == "Unknown Scene":
                    title = scene.get("title") or scene.get("code") or f"Scene {raw_id}"
                
                if scene.get("performers"):
                    performer = ", ".join([p.get("name") for p in scene["performers"]])
                    
                if runtime_ticks <= 0 and scene.get("files"):
                    duration_seconds = scene["files"][0].get("duration", 0)
                    runtime_ticks = float(duration_seconds * 10000000)

        title = title or "Unknown Scene"
        
        if not hasattr(state, "active_streams"):
            state.active_streams = []
            
        stream = next((s for s in state.active_streams if s.get("id") == session_id), None)
        
        if not stream:
            logger.info(f"▶️ PLAYBACK STARTED: Session {session_id} for Item {item_id} ({title}) from {client_ip}")
            stream_info = {
                "id": session_id,
                "item_id": item_id,
                "title": title,
                "performer": performer,
                "runtime_ticks": runtime_ticks,
                "last_ticks": playback_ticks,
                "started": int(time.time()),
                
                # --- 2. INJECT INTO ACTIVE STREAMS CACHE ---
                "user": user,
                "clientIp": client_ip,
                "clientType": client_type
            }
            state.active_streams.append(stream_info)
            
            state.stats["streams_today"] += 1
            state.stats["total_streams"] += 1
            scene_id = item_id if item_id else "unknown"
            if scene_id not in state.stats["top_played"]:
                state.stats["top_played"][scene_id] = {"title": title, "performer": performer, "count": 0}
            state.stats["top_played"][scene_id]["count"] += 1
        else:
            stream["last_ticks"] = max(stream.get("last_ticks", 0), playback_ticks)
            if not stream.get("runtime_ticks") and runtime_ticks > 0:
                stream["runtime_ticks"] = runtime_ticks

    except Exception as e:
        logger.error(f"Error parsing playing session: {e}")
        
    return JSONResponse({}, status_code=204)

async def endpoint_sessions_stopped(request: Request):
    """Receives playback stopped reports to clear active streams and sync watch status."""
    bg_tasks = BackgroundTasks()
    
    try:
        data = await request.json()
        session_id = data.get("PlaySessionId") or data.get("SessionId") or "unknown_session"
        
        if hasattr(state, "active_streams"):
            stream = next((s for s in state.active_streams if s.get("id") == session_id), None)
            if stream:
                state.active_streams = [s for s in state.active_streams if s.get("id") != session_id]
                item_id = decode_id(data.get("ItemId") or data.get("Item", {}).get("Id") or stream.get("item_id", ""))
                
                if item_id.startswith("scene-"):
                    raw_id = item_id.replace("scene-", "")
                    try:
                        reported_ticks = float(data.get("PlaybackPositionTicks") or data.get("PositionTicks") or 0)
                        last_ticks = float(stream.get("last_ticks", 0))
                        playback_ticks = max(reported_ticks, last_ticks)
                        
                        runtime_ticks = float(data.get("RunTimeTicks") or data.get("Item", {}).get("RunTimeTicks") or stream.get("runtime_ticks", 0))
                        
                        if runtime_ticks <= 0:
                            logger.info(f"Client didn't provide RunTimeTicks. Fetching directly from Stash...")
                            scene = stash_client.get_scene(raw_id)
                            if scene and scene.get("files"):
                                duration_seconds = scene["files"][0].get("duration", 0)
                                runtime_ticks = float(duration_seconds * 10000000)
                        
                        if runtime_ticks > 0:
                            percentage = playback_ticks / runtime_ticks
                            logger.info(f"📊 MATH CHECK: played={playback_ticks}, total={runtime_ticks}, pct={percentage*100:.1f}%")
                            
                            if percentage >= 0.90:
                                logger.info(f"✅ Playback reached 90%! Syncing to Stash Play History...")
                                bg_tasks.add_task(_increment_stash_playcount, raw_id)
                                bg_tasks.add_task(_update_stash_resume_time, raw_id, 0)
                            elif percentage > 0.01:
                                resume_seconds = playback_ticks / 10000000.0
                                logger.info(f"⏸️ Playback paused at {percentage*100:.1f}%. Saving resume time...")
                                bg_tasks.add_task(_update_stash_resume_time, raw_id, resume_seconds)
                            else:
                                logger.info(f"❌ Playback stopped at beginning. Clearing resume time.")
                                bg_tasks.add_task(_update_stash_resume_time, raw_id, 0)
                        else:
                            logger.warning(f"Could not determine total duration. Falling back to raw resume time save.")
                            if playback_ticks > 10000000: # > 1 second
                                resume_seconds = playback_ticks / 10000000.0
                                bg_tasks.add_task(_update_stash_resume_time, raw_id, resume_seconds)
                            else:
                                bg_tasks.add_task(_update_stash_resume_time, raw_id, 0)
                            
                    except (ValueError, TypeError) as e:
                        logger.error(f"Failed to calculate playback percentage: {e}")
                else:
                    logger.warning(f"Stop event ignored. ItemId '{item_id}' is not a scene.")
            else:
                logger.warning(f"Stop event received but session '{session_id}' was not found in active streams!")
    except Exception as e:
        logger.error(f"Error processing stopped session: {e}")

    return JSONResponse({}, status_code=204, background=bg_tasks)

async def endpoint_stream(request: Request):
    """Pipes the video stream directly from Stash, supporting DirectPlay and Trojan HLS Playlists."""
    item_id = decode_id(request.path_params.get("item_id", ""))
    raw_id = item_id.replace("scene-", "")
    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    
    # Default to the raw stream
    stash_stream_url = f"{stash_base}/scene/{raw_id}/stream"

    # --- THE ON-THE-FLY HLS INTERCEPT (THE TROJAN PLAYLIST) ---
    graphql_url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    gql_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if apikey:
        gql_headers["ApiKey"] = apikey
        
    is_transcoding = False
    download_ext = "mp4" # Fallback extension
    
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as gql_client:
        try:
            # Quickly query Stash for the video codec AND container format
            query = f"""query {{ findScene(id: "{raw_id}") {{ files {{ video_codec format }} }} }}"""
            resp = await gql_client.post(graphql_url, headers=gql_headers, json={"query": query}, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("findScene", {})
                if data:
                    files = data.get("files", [])
                    v_codec = str(files[0].get("video_codec", "")).lower() if files else ""
                    container = str(files[0].get("format", "")).lower() if files else ""
                    
                    if container:
                        download_ext = container
                    
                    safe_codecs = ["h264", "h265", "hevc", "avc", "vp8", "vp9", "av1"]
                    safe_containers = ["mp4", "m4v", "mov", "webm"]
                    
                    # If it's a legacy codec/container, we must handle it specially
                    if (v_codec and v_codec not in safe_codecs) or (container and container not in safe_containers):
                        
                        if "download" in request.url.path.lower():
                            logger.info(f"📥 Download requested for legacy format! Serving RAW file because download managers reject chunked streams.")
                            
                        else:
                            # --- THE REDIRECT BOUNCE ---
                            # If the app hits /stream, ExoPlayer will try to parse it as an MP4 and hang.
                            # We MUST redirect it to a URL ending in .m3u8 so it triggers the HLS engine.
                            if not request.url.path.lower().endswith(".m3u8"):
                                logger.info(f"🔄 Redirecting strict client to explicit .m3u8 URL for scene {raw_id}")
                                new_url = f"/Videos/{item_id}/master.m3u8"
                                if request.url.query:
                                    new_url += f"?{request.url.query}"
                                return RedirectResponse(url=new_url, status_code=302)

                            logger.info(f"🎥 Serving Trojan HLS Playlist for scene {raw_id}")
                            
                            stash_m3u8_url = f"{stash_base}/scene/{raw_id}/stream.m3u8"
                            if apikey:
                                stash_m3u8_url += f"?apikey={apikey}"
                            
                            m3u8_resp = await gql_client.get(stash_m3u8_url, timeout=10.0)
                            if m3u8_resp.status_code == 200:
                                m3u8_text = m3u8_resp.text
                                rewritten_lines = []
                                
                                for line in m3u8_text.splitlines():
                                    if line.strip() and not line.startswith("#"):
                                        clean_segment = line.split("?")[0].split("/")[-1]
                                        proxy_segment_url = f"/Videos/{item_id}/hls/{clean_segment}"
                                        rewritten_lines.append(proxy_segment_url)
                                    else:
                                        rewritten_lines.append(line)
                                
                                return Response(
                                    content="\n".join(rewritten_lines), 
                                    media_type="application/x-mpegURL",
                                    headers={"Access-Control-Allow-Origin": "*"}
                                )
                            else:
                                logger.error(f"❌ Failed to fetch HLS playlist from Stash: HTTP {m3u8_resp.status_code}")
                            
        except Exception as e:
            logger.warning(f"Failed to check codec for HLS intercept: {e}")

    # --- NORMAL MP4 PASSTHROUGH LOGIC ---
    
    # The Resume Translator
    start_ticks = None
    for k, v in request.query_params.items():
        if k.lower() == "starttimeticks":
            start_ticks = v
            break
            
    if start_ticks:
        try:
            start_sec = float(start_ticks) / 10000000.0
            if "?" in stash_stream_url:
                stash_stream_url += f"&start={start_sec}"
            else:
                stash_stream_url += f"?start={start_sec}"
            logger.info(f"⏭️ Translated Jellyfin start time to {start_sec} seconds")
        except ValueError:
            pass

    # Append the API key correctly
    if apikey and "apikey=" not in stash_stream_url.lower():
        if "?" in stash_stream_url:
            stash_stream_url += f"&apikey={apikey}"
        else:
            stash_stream_url += f"?apikey={apikey}"

    headers = dict(request.headers)
    headers.pop("host", None)
    
    if is_transcoding:
        headers.pop("range", None)
        headers.pop("Range", None)
        range_header = None
    else:
        range_header = headers.get("range") or headers.get("Range")

    async def stream_generator(resp):
        async for chunk in resp.aiter_bytes(chunk_size=8192):
            yield chunk

    client = httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False), timeout=None)
    try:
        req = client.build_request(request.method, stash_stream_url, headers=headers)
        r = await client.send(req, stream=True)

        resp_headers = dict(r.headers)
        resp_headers.pop("content-encoding", None)
        resp_headers.pop("transfer-encoding", None)
        resp_headers.pop("connection", None)
        
        status_code = r.status_code
        if range_header and status_code == 206 and "content-range" not in resp_headers:
            logger.warning(f"Stash returned 206 but missing Content-Range for scene {raw_id}")
            
        if "download" in request.url.path.lower():
            resp_headers["Content-Disposition"] = f'attachment; filename="{raw_id}.{download_ext}"'
        
        if request.method == "HEAD":
            await r.aclose()
            return Response(status_code=status_code, headers=resp_headers)

        response = StreamingResponse(stream_generator(r), status_code=status_code, headers=resp_headers)
        response.background = r.aclose
        return response

    except Exception as e:
        logger.error(f"Stream passthrough failed for scene {raw_id}: {e}")
        return Response(status_code=500)
    
async def endpoint_hls_segment(request: Request):
    """Pipes the individual .ts HLS segments from Stash to the client."""
    item_id = decode_id(request.path_params.get("item_id", ""))
    raw_id = item_id.replace("scene-", "")
    segment = request.path_params.get("segment", "")
    
    stash_base = config.get_stash_base()
    apikey = getattr(config, "STASH_API_KEY", "")
    
    stash_segment_url = f"{stash_base}/scene/{raw_id}/stream.m3u8/{segment}"
    if apikey:
        stash_segment_url += f"?apikey={apikey}"
        
    client = httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False), timeout=None)
    headers = dict(request.headers)
    headers.pop("host", None)
    
    async def stream_generator(resp):
        async for chunk in resp.aiter_bytes(chunk_size=8192):
            yield chunk

    try:
        req = client.build_request(request.method, stash_segment_url, headers=headers)
        r = await client.send(req, stream=True)

        resp_headers = dict(r.headers)
        resp_headers.pop("content-encoding", None)
        resp_headers.pop("transfer-encoding", None)
        resp_headers.pop("connection", None)
        
        response = StreamingResponse(stream_generator(r), status_code=r.status_code, headers=resp_headers)
        response.background = r.aclose
        return response

    except Exception as e:
        logger.error(f"Stream segment passthrough failed for scene {raw_id} segment {segment}: {e}")
        return Response(status_code=500)
          
async def _update_stash_resume_time(raw_id: str, seconds: float):
    stash_base = config.get_stash_base()
    url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if getattr(config, "STASH_API_KEY", ""):
        headers["ApiKey"] = config.STASH_API_KEY
        
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            query = """
            mutation SceneSaveActivity($id: ID!, $resume_time: Float) {
                sceneSaveActivity(id: $id, resume_time: $resume_time)
            }
            """
            resp = await client.post(url, headers=headers, json={"query": query, "variables": {"id": raw_id, "resume_time": seconds}}, timeout=10.0)
            data = resp.json()
            if "errors" in data:
                logger.error(f"❌ Stash rejected the resume time update: {data['errors']}")
            else:
                logger.info(f"✅ Stash accepted SceneSaveActivity! Resume time saved: {seconds}s")
        except Exception as e:
            logger.error(f"Failed to communicate with Stash to update resume time: {e}")

async def _increment_stash_playcount(raw_id: str):
    stash_base = config.get_stash_base()
    url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if getattr(config, "STASH_API_KEY", ""):
        headers["ApiKey"] = config.STASH_API_KEY
        
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            query_play = "mutation($id: ID!) { sceneIncrementPlayCount(id: $id) }"
            await client.post(url, headers=headers, json={"query": query_play, "variables": {"id": raw_id}}, timeout=10.0)
            logger.info(f"Two-Way Sync: Logged official Play event for Scene {raw_id}")
        except Exception as e:
            logger.error(f"Failed to increment play count for Scene {raw_id}: {e}")

async def _increment_stash_o_counter(raw_id: str):
    stash_base = config.get_stash_base()
    url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if getattr(config, "STASH_API_KEY", ""):
        headers["ApiKey"] = config.STASH_API_KEY
        
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            query_o = """
            mutation SceneAddO($id: ID!, $times: [Timestamp!]) {
              sceneAddO(id: $id, times: $times) {
                count
              }
            }
            """
            payload = {"operationName": "SceneAddO", "variables": {"id": raw_id}, "query": query_o}
            resp = await client.post(url, headers=headers, json=payload, timeout=10.0)
            data = resp.json()
            
            if "errors" in data:
                logger.error(f"❌ Stash rejected the O-Counter update: {data['errors']}")
            else:
                new_count = data.get("data", {}).get("sceneAddO", {}).get("count", "Unknown")
                logger.info(f"✅ Two-Way Sync: Added 'O' event for Scene {raw_id}. New Total: {new_count}")
        except Exception as e:
            logger.error(f"Failed to increment O counter for Scene {raw_id}: {e}")

async def endpoint_mark_played(request: Request):
    item_id = decode_id(request.path_params.get("item_id", ""))
    play_count = 1
    task = None
    
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        logger.info(f"✅ 'Mark as Watched' (ON) triggered for {item_id}. Incrementing Play Count.")
        
        # Enqueue the background task
        task = BackgroundTask(_increment_stash_playcount, raw_id)
        
        scene = stash_client.get_scene(raw_id)
        if scene:
            play_count = (scene.get("play_count") or 0) + 1
            
    return JSONResponse({
        "Played": True, 
        "PlayCount": play_count, 
        "PlaybackPositionTicks": 0, 
        "Key": item_id
    }, background=task)

async def endpoint_mark_favorite(request: Request):
    item_id = decode_id(request.path_params.get("item_id", ""))
    played = False
    play_count = 0
    resume_ticks = 0
    task = None
    
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        logger.info(f"💖 Favorite (ON) triggered for {item_id}. Incrementing O-Counter.")
        
        task = BackgroundTask(_increment_stash_o_counter, raw_id)
        
        scene = stash_client.get_scene(raw_id)
        if scene:
            play_count = scene.get("play_count") or 0
            played = play_count > 0
            resume_ticks = int((scene.get("resume_time") or 0) * 10000000)
        
    return JSONResponse({
        "IsFavorite": True, 
        "Played": played,
        "PlayCount": play_count,
        "PlaybackPositionTicks": resume_ticks,
        "Key": item_id
    }, background=task)

async def endpoint_unmark_favorite(request: Request):
    item_id = decode_id(request.path_params.get("item_id", ""))
    played = False
    play_count = 0
    resume_ticks = 0
    task = None
    
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        logger.info(f"💖 Favorite (OFF) triggered for {item_id}. REPURPOSED: Incrementing O-Counter anyway!")
        
        task = BackgroundTask(_increment_stash_o_counter, raw_id)
        
        scene = stash_client.get_scene(raw_id)
        if scene:
            play_count = scene.get("play_count") or 0
            played = play_count > 0
            resume_ticks = int((scene.get("resume_time") or 0) * 10000000)
        
    return JSONResponse({
        "IsFavorite": False, 
        "Played": played,
        "PlayCount": play_count,
        "PlaybackPositionTicks": resume_ticks,
        "Key": item_id
    }, background=task)

async def endpoint_mark_unplayed(request: Request):
    item_id = decode_id(request.path_params.get("item_id", ""))
    play_count = 1
    task = None
    
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        logger.info(f"❌ 'Mark as Unwatched' (OFF) triggered for {item_id}. REPURPOSED: Incrementing Play Count anyway!")
        
        task = BackgroundTask(_increment_stash_playcount, raw_id)
        
        scene = stash_client.get_scene(raw_id)
        if scene:
            play_count = (scene.get("play_count") or 0) + 1
            
    return JSONResponse({
        "Played": False, 
        "PlayCount": play_count, 
        "PlaybackPositionTicks": 0, 
        "Key": item_id
    }, background=task)

async def endpoint_update_userdata(request: Request):
    """Satisfies Fladder's aggressive UserData sync requests before downloading."""
    item_id = decode_id(request.path_params.get("item_id", ""))
    play_count = 0
    is_favorite = False
    played = False
    resume_ticks = 0

    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        scene = stash_client.get_scene(raw_id)
        if scene:
            play_count = scene.get("play_count") or 0
            played = play_count > 0
            is_favorite = (scene.get("o_counter") or 0) > 0
            resume_ticks = int((scene.get("resume_time") or 0) * 10000000)

    return JSONResponse({
        "PlaybackPositionTicks": resume_ticks,
        "PlayCount": play_count,
        "IsFavorite": is_favorite,
        "Played": played,
        "Key": item_id,
        "ItemId": item_id
    })