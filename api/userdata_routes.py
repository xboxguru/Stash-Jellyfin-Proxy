import logging
import time
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.background import BackgroundTasks
import config
from core import stash_client
from core.jellyfin_mapper import decode_id
import state

logger = logging.getLogger(__name__)

def _get_client_info(request: Request, user_agent_fallback: str) -> tuple[str, str]:
    """Responsibility: Extract IP and guess the client type from headers."""
    direct_ip = request.client.host if request.client else "Unknown"
    client_ip = direct_ip
    if bool(getattr(config, "TRUST_PROXY_HEADERS", False)):
        trusted_proxy_ips = set(getattr(config, "TRUSTED_PROXY_IPS", []) or [])
        if not trusted_proxy_ips or direct_ip in trusted_proxy_ips:
            client_ip = request.headers.get("x-forwarded-for", direct_ip).split(",")[0].strip()
    user_agent = request.headers.get("user-agent", user_agent_fallback)
    known_clients = ["Infuse", "Wholphin", "Findroid", "Fladder", "ErsatzTV", "VLC", "Jellyfin"]
    client_type = next((c for c in known_clients if c in user_agent), user_agent.split("/")[0][:20] if user_agent else "Unknown")
    return client_ip, client_type

def _register_new_stream(session_id: str, item_id: str, title: str, performer: str, runtime_ticks: float, playback_ticks: float, user: str, client_ip: str, client_type: str):
    """Responsibility: Mutate state to register a new stream and update global stats."""
    logger.debug(f"Playback started: session={session_id} item={item_id} ip={client_ip} client={client_type}")
    
    state.active_streams.append({
        "id": session_id, "item_id": item_id, "title": title, "performer": performer,
        "runtime_ticks": runtime_ticks, "last_ticks": playback_ticks,
        "started": int(time.time()), "last_ping": int(time.time()), 
        "user": user, "clientIp": client_ip, "clientType": client_type
    })
    
    state.stats["streams_today"] += 1
    state.stats["total_streams"] += 1
    if hasattr(state, "save_stats"): 
        state.save_stats()

async def endpoint_sessions_playing(request: Request):
    try:
        data = await request.json()
        session_id = data.get("PlaySessionId") or data.get("SessionId") or "unknown_session"
        item_id = decode_id(data.get("ItemId") or data.get("Item", {}).get("Id", ""))
        
        playback_ticks = float(data.get("PlaybackPositionTicks") or data.get("PositionTicks") or 0)
        runtime_ticks = float(data.get("RunTimeTicks") or data.get("Item", {}).get("RunTimeTicks") or 0)
        title = data.get("Item", {}).get("Name", "Unknown Scene")
        performer = "Unknown"
        
        client_ip, client_type = _get_client_info(request, data.get("Client", ""))
        user = data.get("UserId") or getattr(config, "SJS_USER", "Admin") or "Admin"
        
        if item_id.startswith("scene-"):
            raw_id = item_id.replace("scene-", "")
            scene = await stash_client.get_scene(raw_id)
            if scene:
                if title == "Unknown Scene": 
                    title = scene.get("title") or scene.get("code") or f"Scene {raw_id}"
                if scene.get("performers"):
                    performer = ", ".join([p.get("name") for p in scene["performers"] if p.get("name")])
                if runtime_ticks <= 0 and scene.get("files"): 
                    runtime_ticks = float(scene["files"][0].get("duration", 0) * 10000000)

        if not hasattr(state, "active_streams"): 
            state.active_streams = []
            
        stream = next((s for s in state.active_streams if s.get("id") == session_id), None)
        
        if not stream:
            _register_new_stream(session_id, item_id, title, performer, runtime_ticks, playback_ticks, user, client_ip, client_type)
        else:
            logger.debug(f"Stream progress update: {session_id} @ {playback_ticks} ticks")
            stream["last_ticks"] = max(stream.get("last_ticks", 0), playback_ticks)
            stream["last_ping"] = int(time.time())
            if not stream.get("runtime_ticks") and runtime_ticks > 0: 
                stream["runtime_ticks"] = runtime_ticks

    except Exception as e: 
        logger.error(f"Error parsing playing session: {e}")
        
    return JSONResponse({}, status_code=204)

def _evaluate_playback_action(playback_ticks: float, runtime_ticks: float) -> tuple[bool, float]:
    if runtime_ticks <= 0:
        return False, (playback_ticks / 10000000.0 if playback_ticks > 10000000 else 0)

    percentage = playback_ticks / runtime_ticks
    if percentage >= 0.90:
        return True, 0.0  
    elif percentage > 0.01:
        return False, playback_ticks / 10000000.0 
    
    return False, 0.0 

async def endpoint_sessions_stopped(request: Request):
    bg_tasks = BackgroundTasks()
    try:
        data = await request.json()
        session_id = data.get("PlaySessionId") or data.get("SessionId") or "unknown_session"
        
        if hasattr(state, "active_streams"):
            stream = next((s for s in state.active_streams if s.get("id") == session_id), None)
            if stream:
                logger.debug(f"Playback stopped: session={session_id}")
                state.active_streams = [s for s in state.active_streams if s.get("id") != session_id]
                item_id = decode_id(data.get("ItemId") or data.get("Item", {}).get("Id") or stream.get("item_id", ""))
                
                if item_id.startswith("scene-"):
                    raw_id = item_id.replace("scene-", "")
                    playback_ticks = max(float(data.get("PlaybackPositionTicks") or data.get("PositionTicks") or 0), float(stream.get("last_ticks", 0)))
                    runtime_ticks = float(data.get("RunTimeTicks") or data.get("Item", {}).get("RunTimeTicks") or stream.get("runtime_ticks", 0))
                    
                    if runtime_ticks <= 0:
                        scene = await stash_client.get_scene(raw_id)
                        if scene and scene.get("files"): runtime_ticks = float(scene["files"][0].get("duration", 0) * 10000000)
                    
                    should_mark_played, resume_time_sec = _evaluate_playback_action(playback_ticks, runtime_ticks)
                    
                    if should_mark_played:
                        bg_tasks.add_task(stash_client.increment_play_count, raw_id)
                    bg_tasks.add_task(stash_client.update_resume_time, raw_id, resume_time_sec)
                    
    except Exception as e: 
        logger.error(f"Error processing stopped session: {e}")
        
    return JSONResponse({}, status_code=204, background=bg_tasks)

async def _toggle_play_state(request: Request, is_played: bool):
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    play_count = 1 if is_played else 0
    bg_tasks = BackgroundTasks()
    is_favorite = False

    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        scene = await stash_client.get_scene(raw_id)
        if scene:
            current_play_count = scene.get("play_count") or 0
            fav_action = getattr(config, "FAVORITE_ACTION", "o_counter").lower()
            is_favorite = (scene.get("rating100") or 0) > 0 if fav_action == "rating" else (scene.get("o_counter") or 0) > 0
            if is_played:
                play_count = current_play_count + 1
                bg_tasks.add_task(stash_client.increment_play_count, raw_id)
            else:
                play_count = max(0, current_play_count - 1)
                if current_play_count > 0:
                    bg_tasks.add_task(stash_client.decrement_play_count, raw_id)
                bg_tasks.add_task(stash_client.update_resume_time, raw_id, 0.0)

    return JSONResponse({
        "IsFavorite": is_favorite,
        "Played": is_played,
        "PlayCount": play_count,
        "PlaybackPositionTicks": 0,
        "Key": raw_item_id if is_played else item_id,
        "ItemId": raw_item_id
    }, background=bg_tasks)

async def _toggle_favorite_state(request: Request, is_favorite: bool):
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    bg_tasks = BackgroundTasks()
    play_count, resume_ticks, played = 0, 0, False
    action = getattr(config, "FAVORITE_ACTION", "o_counter").lower()

    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        
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
                            logger.debug(f"Salvaging watch status for crashed stream {s.get('id')}")
                            import asyncio
                            asyncio.create_task(stash_client.increment_play_count(raw_id))
                        elif resume_seconds > 0:
                            logger.debug(f"Salvaging resume point ({resume_seconds}s) for crashed stream {s.get('id')}")
                            import asyncio
                            asyncio.create_task(stash_client.update_resume_time(raw_id, resume_seconds))
                except Exception as e:
                    logger.error(f"Failed to salvage zombie stream data: {e}")
        else:
            surviving_streams.append(s)
            
    state.active_streams = surviving_streams
    if len(state.active_streams) < original_count:
        logger.debug(f"Pruned {original_count - len(state.active_streams)} zombie streams from memory.")