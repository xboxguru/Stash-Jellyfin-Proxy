import logging
import time
import asyncio
import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import Request
import config
from core import stash_client, jellyfin_mapper
import state

logger = logging.getLogger(__name__)

async def endpoint_playback_info(request: Request):
    """Provides playback info using the robust metadata already built by the mapper."""
    item_id = request.path_params.get("item_id", "")
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
        item_id = data.get("ItemId") or data.get("Item", {}).get("Id", "")
        
        playback_ticks = float(data.get("PlaybackPositionTicks") or data.get("PositionTicks") or 0)
        runtime_ticks = float(data.get("RunTimeTicks") or data.get("Item", {}).get("RunTimeTicks") or 0)
        title = data.get("Item", {}).get("Name") or "Unknown Scene"
        
        if not hasattr(state, "active_streams"):
            state.active_streams = []
            
        stream = next((s for s in state.active_streams if s.get("id") == session_id), None)
        
        if not stream:
            logger.info(f"▶️ PLAYBACK STARTED: Session {session_id} for Item {item_id}")
            stream_info = {
                "id": session_id,
                "item_id": item_id,
                "title": title,
                "runtime_ticks": runtime_ticks,
                "last_ticks": playback_ticks,
                "started": int(time.time())
            }
            state.active_streams.append(stream_info)
            
            state.stats["streams_today"] += 1
            state.stats["total_streams"] += 1
            scene_id = item_id if item_id else "unknown"
            if scene_id not in state.stats["top_played"]:
                state.stats["top_played"][scene_id] = {"title": title, "performer": "Unknown", "count": 0}
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
    try:
        data = await request.json()
        session_id = data.get("PlaySessionId") or data.get("SessionId") or "unknown_session"
        
        if hasattr(state, "active_streams"):
            stream = next((s for s in state.active_streams if s.get("id") == session_id), None)
            if stream:
                state.active_streams = [s for s in state.active_streams if s.get("id") != session_id]
                item_id = data.get("ItemId") or data.get("Item", {}).get("Id") or stream.get("item_id", "")
                
                if item_id.startswith("scene-"):
                    raw_id = item_id.replace("scene-", "")
                    try:
                        reported_ticks = float(data.get("PlaybackPositionTicks") or data.get("PositionTicks") or 0)
                        last_ticks = float(stream.get("last_ticks", 0))
                        playback_ticks = max(reported_ticks, last_ticks)
                        runtime_ticks = float(data.get("RunTimeTicks") or data.get("Item", {}).get("RunTimeTicks") or stream.get("runtime_ticks", 0))
                        
                        if runtime_ticks > 0:
                            percentage = playback_ticks / runtime_ticks
                            logger.info(f"📊 MATH CHECK: played={playback_ticks}, total={runtime_ticks}, pct={percentage*100:.1f}%")
                            
                            if percentage >= 0.90:
                                logger.info(f"✅ Playback reached 90%! Syncing to Stash Play History...")
                                asyncio.create_task(_increment_stash_playcount(raw_id))
                                asyncio.create_task(_update_stash_resume_time(raw_id, 0))
                            elif percentage > 0.01:
                                resume_seconds = playback_ticks / 10000000.0
                                logger.info(f"⏸️ Playback paused at {percentage*100:.1f}%. Saving resume time...")
                                asyncio.create_task(_update_stash_resume_time(raw_id, resume_seconds))
                            else:
                                logger.info(f"❌ Playback stopped at beginning. Clearing resume time.")
                                asyncio.create_task(_update_stash_resume_time(raw_id, 0))
                        else:
                            logger.warning(f"Could not calculate completion. Missing RunTimeTicks. Memory: {stream}")
                            
                    except (ValueError, TypeError) as e:
                        logger.error(f"Failed to calculate playback percentage: {e}")
                else:
                    logger.warning(f"Stop event ignored. ItemId '{item_id}' is not a scene.")
            else:
                logger.warning(f"Stop event received but session '{session_id}' was not found in active streams!")
    except Exception as e:
        logger.error(f"Error processing stopped session: {e}")

    return JSONResponse({}, status_code=204)

async def endpoint_stream(request: Request):
    """Pipes the video stream directly from Stash, fully supporting byte-range seeking."""
    item_id = request.path_params.get("item_id", "")
    raw_id = item_id.replace("scene-", "")
    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
    apikey = getattr(config, "STASH_API_KEY", "")
    
    stash_stream_url = f"{stash_base}/scene/{raw_id}/stream"
    if apikey:
        stash_stream_url += f"?apikey={apikey}"

    headers = dict(request.headers)
    headers.pop("host", None)
    range_header = headers.get("range")

    async def stream_generator(resp):
        async for chunk in resp.aiter_bytes(chunk_size=8192):
            yield chunk

    client = httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False))
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
        
        if request.method == "HEAD":
            await r.aclose()
            return Response(status_code=status_code, headers=resp_headers)

        response = StreamingResponse(stream_generator(r), status_code=status_code, headers=resp_headers)
        response.background = r.aclose
        return response

    except Exception as e:
        logger.error(f"Stream passthrough failed for scene {raw_id}: {e}")
        return Response(status_code=500)

async def _update_stash_resume_time(raw_id: str, seconds: float):
    """Saves the exact playback position to Stash using its native activity tracker."""
    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
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
    """Logs a play in Stash using the official native player mutation."""
    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
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

async def endpoint_mark_played(request: Request):
    """Fired when a user clicks 'Mark as Watched' in the Jellycon context menu."""
    item_id = request.path_params.get("item_id", "")
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        logger.info(f"Manual 'Mark as Watched' triggered for {item_id}")
        asyncio.create_task(_increment_stash_playcount(raw_id))
    return JSONResponse({"Played": True, "PlayCount": 1, "PlaybackPositionTicks": 0, "Key": item_id})