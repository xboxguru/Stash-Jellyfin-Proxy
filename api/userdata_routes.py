import logging
import time
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.background import BackgroundTask, BackgroundTasks
import config
from core import stash_client
from core.jellyfin_mapper import decode_id
import state

logger = logging.getLogger(__name__)

async def endpoint_sessions_playing(request: Request):
    """Receives playback start and progress reports from Jellyfin clients."""
    try:
        data = await request.json()
        session_id = data.get("PlaySessionId") or data.get("SessionId") or "unknown_session"
        item_id = decode_id(data.get("ItemId") or data.get("Item", {}).get("Id", ""))
        
        playback_ticks = float(data.get("PlaybackPositionTicks") or data.get("PositionTicks") or 0)
        runtime_ticks = float(data.get("RunTimeTicks") or data.get("Item", {}).get("RunTimeTicks") or 0)
        title = data.get("Item", {}).get("Name", "Unknown Scene")
        performer = "Unknown"
        
        client_ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "Unknown").split(",")[0].strip()
        user_agent = request.headers.get("user-agent", "")
        client_type = next((c for c in ["Infuse", "Wholphin", "Findroid", "Fladder", "ErsatzTV", "VLC", "Jellyfin"] if c in user_agent), user_agent.split("/")[0][:20] if user_agent else "Unknown")
        user = data.get("UserId") or getattr(config, "SJS_USER", "Admin") or "Admin"
        
        if item_id.startswith("scene-"):
            raw_id = item_id.replace("scene-", "")
            scene = await stash_client.get_scene(raw_id)
            if scene:
                if title == "Unknown Scene": title = scene.get("title") or scene.get("code") or f"Scene {raw_id}"
                if scene.get("performers"): performer = ", ".join([p.get("name") for p in scene["performers"]])
                if runtime_ticks <= 0 and scene.get("files"): runtime_ticks = float(scene["files"][0].get("duration", 0) * 10000000)

        if not hasattr(state, "active_streams"): state.active_streams = []
        stream = next((s for s in state.active_streams if s.get("id") == session_id), None)
        
        if not stream:
            logger.info(f"▶️ PLAYBACK STARTED: Session {session_id} for Item {item_id} ({title}) from {client_ip}")
            state.active_streams.append({
                "id": session_id, "item_id": item_id, "title": title, "performer": performer,
                "runtime_ticks": runtime_ticks, "last_ticks": playback_ticks,
                "started": int(time.time()), "last_ping": int(time.time()), 
                "user": user, "clientIp": client_ip, "clientType": client_type
            })
            
            state.stats["streams_today"] += 1
            state.stats["total_streams"] += 1
            scene_id = item_id if item_id else "unknown"
            if scene_id not in state.stats["top_played"]: 
                state.stats["top_played"][scene_id] = {"title": title, "performer": performer, "count": 0}
            state.stats["top_played"][scene_id]["count"] += 1
            state.stats["top_played"][scene_id]["last_played"] = time.time()
            if hasattr(state, "save_stats"): state.save_stats()
        else:
            stream["last_ticks"] = max(stream.get("last_ticks", 0), playback_ticks)
            stream["last_ping"] = int(time.time())
            if not stream.get("runtime_ticks") and runtime_ticks > 0: stream["runtime_ticks"] = runtime_ticks

    except Exception as e: logger.error(f"Error parsing playing session: {e}")
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
                    playback_ticks = max(float(data.get("PlaybackPositionTicks") or data.get("PositionTicks") or 0), float(stream.get("last_ticks", 0)))
                    runtime_ticks = float(data.get("RunTimeTicks") or data.get("Item", {}).get("RunTimeTicks") or stream.get("runtime_ticks", 0))
                    
                    if runtime_ticks <= 0:
                        scene = await stash_client.get_scene(raw_id)
                        if scene and scene.get("files"): runtime_ticks = float(scene["files"][0].get("duration", 0) * 10000000)
                    
                    if runtime_ticks > 0:
                        percentage = playback_ticks / runtime_ticks
                        if percentage >= 0.90:
                            bg_tasks.add_task(stash_client.increment_play_count, raw_id)
                            bg_tasks.add_task(stash_client.update_resume_time, raw_id, 0)
                        elif percentage > 0.01:
                            bg_tasks.add_task(stash_client.update_resume_time, raw_id, playback_ticks / 10000000.0)
                        else:
                            bg_tasks.add_task(stash_client.update_resume_time, raw_id, 0)
                    elif playback_ticks > 10000000:
                        bg_tasks.add_task(stash_client.update_resume_time, raw_id, playback_ticks / 10000000.0)
                    else:
                        bg_tasks.add_task(stash_client.update_resume_time, raw_id, 0)
    except Exception as e: logger.error(f"Error processing stopped session: {e}")
    return JSONResponse({}, status_code=204, background=bg_tasks)

async def endpoint_mark_played(request: Request):
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    play_count = 1
    task = None
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        task = BackgroundTask(stash_client.increment_play_count, raw_id)
        scene = await stash_client.get_scene(raw_id)
        if scene: play_count = (scene.get("play_count") or 0) + 1
    return JSONResponse({"Played": True, "PlayCount": play_count, "PlaybackPositionTicks": 0, "Key": raw_item_id, "ItemId": raw_item_id}, background=task)

async def endpoint_mark_unplayed(request: Request):
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    play_count = 1
    task = None
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        task = BackgroundTask(stash_client.increment_play_count, raw_id) # Repurposed increment
        scene = await stash_client.get_scene(raw_id)
        if scene: play_count = (scene.get("play_count") or 0) + 1
    return JSONResponse({"Played": False, "PlayCount": play_count, "PlaybackPositionTicks": 0, "Key": item_id, "ItemId": raw_item_id}, background=task)

async def endpoint_mark_favorite(request: Request):
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    bg_tasks = BackgroundTasks()
    play_count, resume_ticks, played = 0, 0, False
    
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        action = getattr(config, "FAVORITE_ACTION", "o_counter").lower()
        
        if action in ["o_counter", "both"]: bg_tasks.add_task(stash_client.increment_o_counter, raw_id)
        if action in ["rating", "both"]: bg_tasks.add_task(stash_client.update_rating, raw_id, 100)
        
        scene = await stash_client.get_scene(raw_id)
        if scene:
            play_count = scene.get("play_count") or 0
            played = play_count > 0
            resume_ticks = int((scene.get("resume_time") or 0) * 10000000)
            
    return JSONResponse({"IsFavorite": True, "Likes": True, "Played": played, "PlayCount": play_count, "PlaybackPositionTicks": resume_ticks, "Key": raw_item_id, "ItemId": raw_item_id}, background=bg_tasks)

async def endpoint_unmark_favorite(request: Request):
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    bg_tasks = BackgroundTasks()
    play_count, resume_ticks, played = 0, 0, False
    
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        action = getattr(config, "FAVORITE_ACTION", "o_counter").lower()
        
        if action in ["o_counter", "both"]: bg_tasks.add_task(stash_client.increment_o_counter, raw_id)
        if action == "both": bg_tasks.add_task(stash_client.update_rating, raw_id, 100)
        elif action == "rating": bg_tasks.add_task(stash_client.update_rating, raw_id, 0)
        
        scene = await stash_client.get_scene(raw_id)
        if scene:
            play_count = scene.get("play_count") or 0
            played = play_count > 0
            resume_ticks = int((scene.get("resume_time") or 0) * 10000000)
            
    return JSONResponse({"IsFavorite": action in ["o_counter", "both"], "Likes": action in ["o_counter", "both"], "Played": played, "PlayCount": play_count, "PlaybackPositionTicks": resume_ticks, "Key": raw_item_id, "ItemId": raw_item_id}, background=bg_tasks)

async def endpoint_update_userdata(request: Request):
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    play_count, resume_ticks, played, is_favorite = 0, 0, False, False

    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        scene = await stash_client.get_scene(raw_id)
        if scene:
            play_count = scene.get("play_count") or 0
            played = play_count > 0
            resume_ticks = int((scene.get("resume_time") or 0) * 10000000)
            fav_action = getattr(config, "FAVORITE_ACTION", "o_counter").lower()
            is_favorite = (scene.get("rating100") or 0) > 0 if fav_action == "rating" else (scene.get("o_counter") or 0) > 0

    return JSONResponse({"PlaybackPositionTicks": resume_ticks, "PlayCount": play_count, "IsFavorite": is_favorite, "Played": played, "Key": raw_item_id, "ItemId": raw_item_id})