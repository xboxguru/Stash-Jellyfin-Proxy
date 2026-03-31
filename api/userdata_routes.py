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

# --- REFACTORED HELPER ---
def _evaluate_playback_action(playback_ticks: float, runtime_ticks: float) -> tuple[bool, float]:
    """
    Responsibility: Calculate if a scene should be marked played, or just update resume time.
    Returns: (should_mark_played, resume_seconds)
    """
    if runtime_ticks <= 0:
        return False, (playback_ticks / 10000000.0 if playback_ticks > 10000000 else 0)

    percentage = playback_ticks / runtime_ticks
    if percentage >= 0.90:
        return True, 0.0  # Mark played, clear resume time
    elif percentage > 0.01:
        return False, playback_ticks / 10000000.0 # Save resume time
    
    return False, 0.0 # Watched too little, clear resume time
# -------------------------

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
                    
                    # Delegate logic to helper function
                    should_mark_played, resume_time_sec = _evaluate_playback_action(playback_ticks, runtime_ticks)
                    
                    if should_mark_played:
                        bg_tasks.add_task(stash_client.increment_play_count, raw_id)
                    bg_tasks.add_task(stash_client.update_resume_time, raw_id, resume_time_sec)
                    
    except Exception as e: logger.error(f"Error processing stopped session: {e}")
    return JSONResponse({}, status_code=204, background=bg_tasks)

async def _toggle_play_state(request: Request, is_played: bool):
    """Responsibility: Handle both marking and unmarking items as played."""
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    play_count = 1
    task = None
    
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        # Even if unplayed, the proxy currently increments to trigger a Stash UI update
        task = BackgroundTask(stash_client.increment_play_count, raw_id) 
        scene = await stash_client.get_scene(raw_id)
        if scene: play_count = (scene.get("play_count") or 0) + 1
        
    return JSONResponse({
        "Played": is_played, 
        "PlayCount": play_count, 
        "PlaybackPositionTicks": 0, 
        "Key": raw_item_id if is_played else item_id, # Replicated original proxy quirk
        "ItemId": raw_item_id
    }, background=task)

async def _toggle_favorite_state(request: Request, is_favorite: bool):
    """Responsibility: Handle both favoriting and unfavoriting items."""
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    bg_tasks = BackgroundTasks()
    play_count, resume_ticks, played = 0, 0, False
    
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        action = getattr(config, "FAVORITE_ACTION", "o_counter").lower()
        
        # Add tasks based on config action
        if action in ["o_counter", "both"]: 
            bg_tasks.add_task(stash_client.increment_o_counter, raw_id)
            
        if action == "both": 
            bg_tasks.add_task(stash_client.update_rating, raw_id, 100)
        elif action == "rating": 
            bg_tasks.add_task(stash_client.update_rating, raw_id, 100 if is_favorite else 0)
        
        scene = await stash_client.get_scene(raw_id)
        if scene:
            play_count = scene.get("play_count") or 0
            played = play_count > 0
            resume_ticks = int((scene.get("resume_time") or 0) * 10000000)
            
    # Stash-Infuse proxy traditionally returns True for "Likes" if O-counter is used
    likes_state = True if (is_favorite and action in ["o_counter", "both"]) else (action in ["o_counter", "both"])
            
    return JSONResponse({
        "IsFavorite": is_favorite if action == "rating" else likes_state, 
        "Likes": likes_state, 
        "Played": played, 
        "PlayCount": play_count, 
        "PlaybackPositionTicks": resume_ticks, 
        "Key": raw_item_id, 
        "ItemId": raw_item_id
    }, background=bg_tasks)


async def endpoint_mark_played(request: Request):
    return await _toggle_play_state(request, True)

async def endpoint_mark_unplayed(request: Request):
    return await _toggle_play_state(request, False)

async def endpoint_mark_favorite(request: Request):
    return await _toggle_favorite_state(request, True)

async def endpoint_unmark_favorite(request: Request):
    return await _toggle_favorite_state(request, False)
    
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

async def prune_and_salvage_zombie_streams():
    """
    Responsibility: Identify streams that haven't pinged in 15 minutes, 
    salvage their resume/watch data via Stash GraphQL, and remove them from memory.
    """
    if not hasattr(state, "active_streams") or not state.active_streams:
        return

    current_time = time.time()
    original_count = len(state.active_streams)
    surviving_streams = []
    
    for s in state.active_streams:
        if current_time - s.get("last_ping", s.get("started", current_time)) >= 900:
            item_id = s.get("item_id", "")
            if item_id.startswith("scene-"):
                raw_id = item_id.replace("scene-", "")
                try:
                    last_ticks = float(s.get("last_ticks", 0))
                    runtime_ticks = float(s.get("runtime_ticks", 0))
                    
                    if runtime_ticks > 0:
                        should_mark_played, resume_seconds = _evaluate_playback_action(last_ticks, runtime_ticks)
                        
                        if should_mark_played:
                            logger.info(f"🧟 Salvaging watch status for crashed stream {s.get('id')}")
                            import asyncio
                            asyncio.create_task(stash_client.increment_play_count(raw_id))
                        elif resume_seconds > 0:
                            logger.info(f"🧟 Salvaging resume point ({resume_seconds}s) for crashed stream {s.get('id')}")
                            import asyncio
                            asyncio.create_task(stash_client.update_resume_time(raw_id, resume_seconds))
                except Exception as e:
                    logger.error(f"Failed to salvage zombie stream data: {e}")
        else:
            surviving_streams.append(s)
            
    state.active_streams = surviving_streams
    if len(state.active_streams) < original_count:
        logger.info(f"🧹 Pruned {original_count - len(state.active_streams)} zombie streams from memory.")