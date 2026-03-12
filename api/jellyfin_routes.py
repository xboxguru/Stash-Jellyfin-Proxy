import logging
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.requests import Request
import config
from core import stash_client
from core import jellyfin_mapper
import asyncio
import httpx
from starlette.responses import StreamingResponse
import re

logger = logging.getLogger(__name__)

def _get_query_param(request: Request, param_name: str, default=None):
    """Case-insensitive query parameter extraction to handle picky clients."""
    for k, v in request.query_params.items():
        if k.lower() == param_name.lower():
            return v
    return default

async def endpoint_items(request: Request):
    """Handles requests for multiple items with concurrent fetching to prevent timeouts."""
    parent_id = _get_query_param(request, "ParentId")
    ids_param = _get_query_param(request, "Ids")
    
    try:
        start_index = int(_get_query_param(request, "StartIndex", 0))
    except (ValueError, TypeError):
        start_index = 0
        
    try:
        limit = int(_get_query_param(request, "Limit", getattr(config, "DEFAULT_PAGE_SIZE", 50)))
    except (ValueError, TypeError):
        limit = getattr(config, "DEFAULT_PAGE_SIZE", 50)

    # 1. ERSATZTV WANTS SPECIFIC SCENES (CONCURRENT FETCH)
    if ids_param:
        raw_ids = [i.replace("scene-", "") for i in ids_param.split(",")]
        jellyfin_items = []
        
        stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
        url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if getattr(config, "STASH_API_KEY", ""):
            headers["ApiKey"] = config.STASH_API_KEY
        
        # Create a semaphore to limit concurrent Stash requests to 10 at a time
        semaphore = asyncio.Semaphore(10)
        
        async def fetch_single_scene(client, raw_id):
            async with semaphore:  # <-- This safely throttles the requests!
                query = f"""
                query FindScene($id: ID!) {{
                    findScene(id: $id) {{
                        {stash_client.SCENE_FIELDS}
                    }}
                }}
                """
                try:
                    resp = await client.post(url, headers=headers, json={"query": query, "variables": {"id": raw_id}}, timeout=10.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data and "data" in data and data["data"].get("findScene"):
                            return data["data"]["findScene"]
                except Exception as e:
                    logger.error(f"Concurrent fetch failed for scene {raw_id}: {e}")
                return None

        # Fire all 100 requests to Stash at the exact same time
        async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
            tasks = [fetch_single_scene(client, rid) for rid in raw_ids]
            results = await asyncio.gather(*tasks)
            
        # Map the results
        for scene in results:
            if scene:
                try:
                    jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or "root-scenes"))
                except Exception as e:
                    logger.error(f"Failed to map scene during bulk fetch: {e}")
        
        return JSONResponse({
            "Items": jellyfin_items,
            "TotalRecordCount": len(jellyfin_items),
            "StartIndex": 0
        })

   # 2. NORMAL PAGINATED SEARCH
    filter_args = {
        "sort": "created_at",
        "direction": "DESC"
    }
    scene_filter = {}  # This gets passed to Stash's SceneFilterType
    original_limit = limit

    # --- JELLYCON FILTER ENGINE ---
    # 0. PREVENT MENU LEAKAGE (Like Collections/Boxsets showing all movies)
    item_types = _get_query_param(request, "IncludeItemTypes", "").lower()
    if item_types:
        allowed = False
        # If they specifically ask for movies or series, let it through
        for t in ["movie", "video", "series", "episode"]:
            if t in item_types:
                allowed = True
                break
        if not allowed:
            # They asked for BoxSets, Playlists, or Persons. We don't map those to items!
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})

    # 1. Sort By & Sort Order
    sort_by = _get_query_param(request, "SortBy", "").lower()
    if "random" in sort_by:
        filter_args["sort"] = "random"
    elif "datecreated" in sort_by:
        filter_args["sort"] = "created_at"
    elif "dateplayed" in sort_by:
        filter_args["sort"] = "updated_at" # Map Jellyfin's DatePlayed to Stash's Updated_At
    elif "name" in sort_by or "sortname" in sort_by:
        filter_args["sort"] = "title"
        
    sort_order = _get_query_param(request, "SortOrder", "").lower()
    if sort_order == "ascending":
        filter_args["direction"] = "ASC"

    # 2. Watch Status & Favorites
    filters_string = _get_query_param(request, "Filters", "")
    # Split by comma so we check exact words, avoiding the "IsPlayed" in "IsUnplayed" trap!
    filter_list = [f.strip() for f in filters_string.split(",")] 

    if "IsUnplayed" in filter_list:
        scene_filter["play_count"] = {"value": 0, "modifier": "EQUALS"}
    elif "IsPlayed" in filter_list:
        scene_filter["play_count"] = {"value": 0, "modifier": "GREATER_THAN"}
        
    if "IsFavorite" in filter_list:
        scene_filter["o_counter"] = {"value": 0, "modifier": "GREATER_THAN"}
        
    # In Progress - Use memory filtering because Stash doesn't index resume_time!
    if "IsResumable" in filter_list:
        filter_args["sort"] = "updated_at" 
        filter_args["direction"] = "DESC"
        limit = -1

    # 3. Years & Decades
    years = _get_query_param(request, "Years")
    if years:
        year_list = [int(y) for y in years.split(",") if y.isdigit()]
        if year_list:
            min_year = min(year_list)
            max_year = max(year_list)
            scene_filter["date"] = {
                "value": f"{min_year}-01-01",
                "value2": f"{max_year}-12-31",
                "modifier": "BETWEEN"
            }

    # 4. Tags & Genres
    tags_param = _get_query_param(request, "Tags") or _get_query_param(request, "TagIds") or _get_query_param(request, "GenreIds")
    if tags_param:
        raw_tags = [t.replace("tag-", "") for t in tags_param.split(",") if t.replace("tag-", "").isdigit()]
        if raw_tags:
            scene_filter["tags"] = {"value": raw_tags, "modifier": "INCLUDES"}

    # 5. Letters & Global Search
    name_starts = _get_query_param(request, "NameStartsWith")
    search_term = _get_query_param(request, "SearchTerm")
    if name_starts:
        filter_args["q"] = name_starts
        filter_args["sort"] = "title"
        filter_args["direction"] = "ASC"
        limit = -1 
    elif search_term:
        filter_args["q"] = search_term

    # Execute Search!
    if original_limit == 0 and not name_starts:
        stash_data = stash_client.fetch_scenes(filter_args, page=1, per_page=1, scene_filter=scene_filter)
        return JSONResponse({
            "Items": [],
            "TotalRecordCount": stash_data.get("count", 0),
            "StartIndex": start_index
        })

    page = (start_index // limit) + 1 if limit > 0 else 1
    
    # We pass the newly built scene_filter to Stash!
    stash_data = stash_client.fetch_scenes(filter_args, page=page, per_page=limit, scene_filter=scene_filter)
    
    jellyfin_items = []
    for scene in stash_data.get("scenes", []):
        # Strict Python filtering for Letters
        if name_starts:
            title = scene.get("title") or scene.get("code") or ""
            if not title.lower().startswith(name_starts.lower()):
                continue

        # Strict Python filtering for In Progress
        if "IsResumable" in filter_list:
            if not scene.get("resume_time") or scene.get("resume_time") <= 0:
                continue

        try:
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or "root-scenes"))
        except Exception as e:
            logger.error(f"Failed to map scene during pagination: {e}")

    total_count = stash_data.get("count", 0)
    if name_starts or "IsResumable" in filter_list:
        total_count = len(jellyfin_items)
        if original_limit > 0:
            jellyfin_items = jellyfin_items[start_index : start_index + original_limit]

    return JSONResponse({
        "Items": jellyfin_items,
        "TotalRecordCount": total_count,
        "StartIndex": start_index
    })

def _get_libraries():
    """Matches the exact 'VirtualFolder' schema."""
    server_id = getattr(config, "SERVER_ID", "")
    
    views = [
        {
            "Name": "Scenes",
            "Id": "root-scenes",
            "Guid": "root-scenes",
            "ServerId": server_id,
            "CollectionType": "movies",
            "Type": "CollectionFolder",
            "ItemId": "root-scenes",
            
            # Tunarr Strict Schema Requirements
            "LibraryOptions": {
                "PathInfos": []
            },
            "Locations": []
        }
    ]
    
    for tag in getattr(config, "TAG_GROUPS", []):
        if tag.strip():
            t = tag.strip()
            views.append({
                "Name": t,
                "Id": f"tag-{t}",
                "Guid": f"tag-{t}",
                "ServerId": server_id,
                "CollectionType": "movies",
                "Type": "CollectionFolder",
                "ItemId": f"tag-{t}",
                
                # Tunarr Strict Schema Requirements
                "LibraryOptions": {
                    "PathInfos": []
                },
                "Locations": []
            })
            
    return views

async def endpoint_views(request: Request):
    """
    Jellycon STRICTLY expects this to be an object with an Items array.
    (ErsatzTV/Tunarr usually rely on /Library/VirtualFolders for the raw array instead).
    """
    views = _get_libraries()
    return JSONResponse({
        "Items": views,
        "TotalRecordCount": len(views),
        "StartIndex": 0
    })

async def endpoint_virtual_folders(request: Request):
    """Returns libraries as a raw Array with strict header enforcement."""
    views = _get_libraries()
    return JSONResponse(
        content=views, 
        headers={"Content-Type": "application/json; charset=utf-8"}
    )

async def endpoint_item_details(request: Request):
    """Handles requests for a single specific item."""
    item_id = request.path_params.get("item_id", "")
    
    # PREVENT CRASH: If Jellycon asks for a folder, hand it a dummy object
    if item_id == "root-scenes" or item_id.startswith("tag-"):
        return JSONResponse({"Name": "Folder", "Id": item_id, "Type": "CollectionFolder", "IsFolder": True})
        
    raw_id = item_id.replace("scene-", "")
    
    # PROTECT STASH: If Jellycon asks for a menu string, reject it cleanly
    if not raw_id.isdigit():
        return JSONResponse({"error": "Item not found"}, status_code=404)
    
    # Fetch the single scene from Stash
    scene = stash_client.get_scene(raw_id)
    
    if not scene:
        return JSONResponse({"error": "Item not found"}, status_code=404)
        
    jellyfin_item = jellyfin_mapper.format_jellyfin_item(scene)
    return JSONResponse(jellyfin_item)

async def endpoint_playback_info(request: Request):
    """Provides playback info using the robust metadata already built by the mapper."""
    item_id = request.path_params.get("item_id", "")
    raw_id = item_id.replace("scene-", "")
    
    scene = stash_client.get_scene(raw_id)
    if not scene:
        return JSONResponse({"error": "Item not found"}, status_code=404)
        
    jellyfin_item = jellyfin_mapper.format_jellyfin_item(scene)
    
    # We simply return the MediaSources array that the mapper just built
    playback_payload = {
        "MediaSources": jellyfin_item.get("MediaSources", []),
        "PlaySessionId": f"stash_{raw_id}"
    }

    return JSONResponse(playback_payload)

async def endpoint_sessions_playing(request: Request):
    """Receives playback start and progress reports from Jellyfin clients."""
    import state
    import time
    
    try:
        data = await request.json()
        
        # Aggressively hunt for IDs (clients format these differently)
        session_id = data.get("PlaySessionId") or data.get("SessionId") or "unknown_session"
        item_id = data.get("ItemId") or data.get("Item", {}).get("Id", "")
        
        # FIX: Check for both PlaybackPositionTicks and PositionTicks
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
                "item_id": item_id,  # SAVE ITEM ID TO MEMORY!
                "title": title,
                "runtime_ticks": runtime_ticks,
                "last_ticks": playback_ticks,
                "started": int(time.time())
            }
            state.active_streams.append(stream_info)
            
            # Stats tracking
            state.stats["streams_today"] += 1
            state.stats["total_streams"] += 1
            scene_id = item_id if item_id else "unknown"
            if scene_id not in state.stats["top_played"]:
                state.stats["top_played"][scene_id] = {"title": title, "performer": "Unknown", "count": 0}
            state.stats["top_played"][scene_id]["count"] += 1
        else:
            # UPDATE MEMORY during progress heartbeats
            stream["last_ticks"] = max(stream.get("last_ticks", 0), playback_ticks)
            if not stream.get("runtime_ticks") and runtime_ticks > 0:
                stream["runtime_ticks"] = runtime_ticks

    except Exception as e:
        logger.error(f"Error parsing playing session: {e}")
        
    return JSONResponse({}, status_code=204)


async def endpoint_sessions_stopped(request: Request):
    """Receives playback stopped reports to clear active streams and sync watch status."""
    import state
    
    try:
        data = await request.json()
        logger.info(f"🛑 RAW STOP PAYLOAD: {data}") # DEBUG: See exactly what the client sent
        
        session_id = data.get("PlaySessionId") or data.get("SessionId") or "unknown_session"
        
        if hasattr(state, "active_streams"):
            stream = next((s for s in state.active_streams if s.get("id") == session_id), None)
            
            if stream:
                # Remove the stream from the dashboard
                state.active_streams = [s for s in state.active_streams if s.get("id") != session_id]
                
                # Fetch ID from the payload, OR fall back to the proxy's memory
                item_id = data.get("ItemId") or data.get("Item", {}).get("Id") or stream.get("item_id", "")
                
                if item_id.startswith("scene-"):
                    raw_id = item_id.replace("scene-", "")
                    
                    try:
                        # FIX: Check for both PlaybackPositionTicks and PositionTicks
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
                                
                                # Clear the resume time since the video is finished!
                                asyncio.create_task(_update_stash_resume_time(raw_id, 0))
                                
                            elif percentage > 0.01:
                                # They watched more than 1%, but didn't finish. Save the spot!
                                resume_seconds = playback_ticks / 10000000.0
                                logger.info(f"⏸️ Playback paused at {percentage*100:.1f}%. Saving resume time...")
                                asyncio.create_task(_update_stash_resume_time(raw_id, resume_seconds))
                            else:
                                # They stopped immediately (0%). Clear any existing resume times.
                                logger.info(f"❌ Playback stopped at beginning. Clearing resume time.")
                                asyncio.create_task(_update_stash_resume_time(raw_id, 0))
                        else:
                            logger.warning(f"Could not calculate completion. Missing RunTimeTicks. Memory: {stream}")
                            
                    except (ValueError, TypeError) as e:
                        logger.error(f"Failed to calculate playback percentage due to invalid data format: {e}")
                else:
                    logger.warning(f"Stop event ignored. ItemId '{item_id}' is not a scene.")
            else:
                logger.warning(f"Stop event received but session '{session_id}' was not found in active streams!")
            
    except Exception as e:
        logger.error(f"Error processing stopped session: {e}")

    return JSONResponse({}, status_code=204)

async def endpoint_public_users(request: Request):
    """Jellycon uses this to list users on the login screen."""
    # Strict 32-character hex ID required by Jellyfin clients
    valid_jellyfin_id = "00000000000000000000000000000001" 
    
    return JSONResponse([{
        "Name": getattr(config, "SJS_USER", "admin") or "admin",
        "ServerId": getattr(config, "SERVER_ID", ""),
        "Id": valid_jellyfin_id,
        "HasPassword": bool(getattr(config, "SJS_PASSWORD", "")),
        "HasConfiguredPassword": bool(getattr(config, "SJS_PASSWORD", "")),
        "HasConfiguredEasyPassword": False
    }])

async def endpoint_user(request: Request):
    """Returns the user details when Jellycon verifies the login."""
    # Must perfectly match the ID from authentication and public users
    valid_jellyfin_id = "00000000000000000000000000000001"
    
    return JSONResponse({
        "Name": getattr(config, "SJS_USER", "admin") or "admin",
        "Id": valid_jellyfin_id,
        "ServerId": getattr(config, "SERVER_ID", ""),
        "Policy": {
            "IsAdministrator": True,
            "IsHidden": False,
            "IsDisabled": False,
            "MaxParentalRating": None,
        }
    })

async def endpoint_authenticate_by_name(request: Request):
    """Authenticates the user and hands the client our Proxy API Key."""
    try:
        data = await request.json()
    except Exception:
        data = {}
        
    # Clients use different capitalizations, so we check a few
    username = data.get("Username") or data.get("username") or ""
    password = data.get("Pw") or data.get("pw") or data.get("Password") or ""
    
    expected_user = str(getattr(config, "SJS_USER", "")).strip()
    expected_pass = str(getattr(config, "SJS_PASSWORD", "")).strip()
    
    # 1. ENFORCE SECURITY: Check credentials
    if expected_user:
        if username.lower() != expected_user.lower() or password != expected_pass:
            logger.warning(f"Failed login attempt for user: {username}")
            return JSONResponse({"error": "Invalid username or password"}, status_code=401)
            
    # 2. Login successful! Hand them the master key.
    # Jellyfin clients STRICTLY require a 32-character hex ID.
    valid_jellyfin_id = "00000000000000000000000000000001"
    
    fake_user = {
        "Name": expected_user or "admin",
        "ServerId": getattr(config, "SERVER_ID", ""),
        "Id": valid_jellyfin_id,
        "HasPassword": bool(expected_pass),
        "Policy": {"IsAdministrator": True}
    }
    
    return JSONResponse({
        "User": fake_user,
        "SessionInfo": {
            "UserId": valid_jellyfin_id,
            "Id": "00000000000000000000000000000002" # Session ID can be any 32-char hex
        },
        "AccessToken": config.PROXY_API_KEY,
        "ServerId": getattr(config, "SERVER_ID", "")
    })

async def endpoint_users(request: Request):
    """Returns a fake user list containing our single proxy user."""
    valid_jellyfin_id = "00000000000000000000000000000001"
    
    user = {
        "Name": getattr(config, "SJS_USER", "admin") or "admin",
        "ServerId": getattr(config, "SERVER_ID", ""),
        "Id": valid_jellyfin_id,
        "Policy": {"IsAdministrator": True}
    }
    return JSONResponse([user])

async def endpoint_system_info_public(request: Request):
    # Dynamically grab the exact IP/Host the client is using to reach us
    host = request.headers.get("host", f"127.0.0.1:{getattr(config, 'PROXY_PORT', 8096)}")
    scheme = request.url.scheme
    return JSONResponse({
        "LocalAddress": f"{scheme}://{host}",
        "ServerName": getattr(config, "SERVER_NAME", "Stash Proxy") or "Stash Proxy",
        "Version": "10.8.10",
        "Id": getattr(config, "SERVER_ID", "") or "stash-proxy-server-id-01"
    })

async def endpoint_system_info(request: Request):
    host = request.headers.get("host", f"127.0.0.1:{getattr(config, 'PROXY_PORT', 8096)}")
    scheme = request.url.scheme
    return JSONResponse({
        "LocalAddress": f"{scheme}://{host}",
        "ServerName": getattr(config, "SERVER_NAME", "Stash Proxy") or "Stash Proxy",
        "Version": "10.8.10",
        "Id": getattr(config, "SERVER_ID", "") or "stash-proxy-server-id-01",
        "OperatingSystem": "Linux"
    })

async def endpoint_quickconnect_enabled(request: Request):
    """Tells clients that QuickConnect is disabled, forcing standard login."""
    return JSONResponse(False)

async def endpoint_quickconnect_initiate(request: Request):
    """Explicitly reject QuickConnect so Jellycon falls back to manual password entry."""
    return JSONResponse({"error": "QuickConnect is not supported on this proxy."}, status_code=400)

async def endpoint_item_image(request: Request):
    """Downloads the image from Stash and serves it directly to ErsatzTV."""
    item_id = request.path_params.get("item_id", "")
    
    logger.info(f"IMAGE REQUEST: ErsatzTV is asking for {item_id}")
    
    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
    params = {}
    if getattr(config, "STASH_API_KEY", ""):
        params["apikey"] = config.STASH_API_KEY

    # 1. Route to the correct Stash image endpoint based on the prefix
    if item_id.startswith("person-"):
        raw_id = item_id.replace("person-", "")
        url = f"{stash_base}/performer/{raw_id}/image"
    elif item_id.startswith("studio-"):
        raw_id = item_id.replace("studio-", "")
        url = f"{stash_base}/studio/{raw_id}/image"
    else:
        # Default to scene screenshot
        raw_id = item_id.replace("scene-", "")
        url = f"{stash_base}/scene/{raw_id}/screenshot"
        
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            logger.info(f"IMAGE RESPONSE: Stash returned HTTP {resp.status_code} for {item_id}")
            
            if resp.status_code == 200:
                return Response(
                    content=resp.content, 
                    media_type=resp.headers.get("Content-Type", "image/jpeg")
                )
            else:
                return Response(status_code=404)
        except Exception as e:
            logger.error(f"IMAGE ERROR: Failed to proxy image from Stash: {e}")
            return Response(status_code=500)
        
async def endpoint_studios(request: Request):
    """Returns all Stash studios as Jellyfin Studio objects."""
    studios = stash_client.get_all_studios()
    img_version = getattr(config, "IMAGE_VERSION", 0)
    
    jelly_studios = []
    for s in studios:
        s_id = s.get("id")
        s_name = s.get("name")
        s_tag = f"s-{s_id}-v{img_version}"
        
        jelly_studios.append({
            "Name": s_name,
            "Id": f"studio-{s_id}",
            "Type": "Studio",
            "ImageTags": {"Primary": s_tag},
            "HasPrimaryImage": bool(s.get("image_path"))
        })

    return JSONResponse({
        "Items": jelly_studios,
        "TotalRecordCount": len(jelly_studios),
        "StartIndex": 0
    })

async def endpoint_system_ping(request: Request):
    """Answers Tunarr server health checks."""
    return PlainTextResponse("Jellyfin Server")

async def endpoint_empty_list(request: Request):
    """Returns an empty Jellyfin list response for unsupported menu items."""
    return JSONResponse({
        "Items": [],
        "TotalRecordCount": 0,
        "StartIndex": 0
    })

async def endpoint_stream(request: Request):
    """Pipes the video stream directly from Stash, fully supporting byte-range seeking."""
    item_id = request.path_params.get("item_id", "")
    raw_id = item_id.replace("scene-", "")

    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
    apikey = getattr(config, "STASH_API_KEY", "")
    
    # Target the Stash native streaming endpoint
    stash_stream_url = f"{stash_base}/scene/{raw_id}/stream"
    if apikey:
        stash_stream_url += f"?apikey={apikey}"

    # Safely extract headers and clean them up for Stash
    headers = dict(request.headers)
    headers.pop("host", None)
    
    # Kodi relies heavily on the 'Range' header for seeking and buffering
    range_header = headers.get("range")

    async def stream_generator(resp):
        """Yields chunks of the video directly to the client."""
        async for chunk in resp.aiter_bytes(chunk_size=8192):
            yield chunk

    client = httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False))
    
    try:
        # We MUST use send() with stream=True so we don't load a 10GB file into RAM
        req = client.build_request(request.method, stash_stream_url, headers=headers)
        r = await client.send(req, stream=True)

        # Prepare the response headers to bounce back to Kodi
        resp_headers = dict(r.headers)
        
        # Security/CORS cleanup: Remove headers that might confuse Starlette/Kodi
        resp_headers.pop("content-encoding", None)
        resp_headers.pop("transfer-encoding", None)
        resp_headers.pop("connection", None)
        
        # Default to 200 OK
        status_code = r.status_code
        
        # CRITICAL: If Kodi asked for a range, we MUST return 206 Partial Content
        if range_header and status_code == 206:
            # Ensure the Content-Range header survived the trip from Stash
            if "content-range" not in resp_headers:
                logger.warning(f"Stash returned 206 but missing Content-Range for scene {raw_id}")
        
        # If it's a HEAD request (Kodi checking file size), don't stream the body
        if request.method == "HEAD":
            await r.aclose()
            return Response(status_code=status_code, headers=resp_headers)

        # Return the StreamingResponse, tying its lifecycle to the httpx response
        response = StreamingResponse(
            stream_generator(r), 
            status_code=status_code, 
            headers=resp_headers
        )
        
        # Ensure the httpx client closes when the streaming response finishes/disconnects
        response.background = r.aclose
        return response

    except Exception as e:
        logger.error(f"Stream passthrough failed for scene {raw_id}: {e}")
        return Response(status_code=500)

async def endpoint_tags(request: Request):
    """Fetches all tags from Stash and formats them for the Jellycon menu."""
    # Check if Jellycon asked for Genres or Tags
    is_genre = "genre" in request.url.path.lower()
    item_type = "Genre" if is_genre else "Tag"

    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
    url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if getattr(config, "STASH_API_KEY", ""):
        headers["ApiKey"] = config.STASH_API_KEY
        
    # FIX: sort and direction MUST be in the FindFilterType (filter), not the TagFilterType!
    query = """
    query {
        findTags(filter: {per_page: -1, sort: "name", direction: ASC}) {
            tags { id name }
        }
    }
    """
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            resp = await client.post(url, headers=headers, json={"query": query}, timeout=10.0)
            stash_tags = resp.json().get("data", {}).get("findTags", {}).get("tags", [])
        except Exception as e:
            logger.error(f"Failed to fetch tags: {e}")
            stash_tags = []

    jelly_tags = [{"Name": t.get("name"), "Id": f"tag-{t.get('id')}", "Type": item_type} for t in stash_tags]

    return JSONResponse({"Items": jelly_tags, "TotalRecordCount": len(jelly_tags), "StartIndex": 0})


async def endpoint_years(request: Request):
    """Returns a dynamic list of years for the Jellycon menu."""
    import datetime
    current_year = datetime.datetime.now().year
    years = []
    
    # Generate years from current down to 1990
    for y in range(current_year, 1989, -1):
        years.append({"Name": str(y), "Id": str(y), "Type": "Year", "ProductionYear": y})

    return JSONResponse({"Items": years, "TotalRecordCount": len(years), "StartIndex": 0})

async def _update_stash_resume_time(raw_id: str, seconds: float):
    """Saves the exact playback position to Stash using its native activity tracker."""
    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
    url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if getattr(config, "STASH_API_KEY", ""):
        headers["ApiKey"] = config.STASH_API_KEY
        
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            # We now use the EXACT mutation the Stash Web UI uses!
            query = """
            mutation SceneSaveActivity($id: ID!, $resume_time: Float) {
                sceneSaveActivity(id: $id, resume_time: $resume_time)
            }
            """
            variables = {
                "id": raw_id,
                "resume_time": seconds
            }
            
            resp = await client.post(url, headers=headers, json={"query": query, "variables": variables}, timeout=10.0)
            data = resp.json()
            
            # STRICT ERROR CHECKING
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
            # Use the exact mutation Stash's native web player uses!
            # This specifically logs a 'Play' in history WITHOUT triggering an 'O'
            query_play = """
            mutation($id: ID!) {
                sceneIncrementPlayCount(id: $id)
            }
            """
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

async def endpoint_latest(request: Request):
    """Feeds the 'Latest' ribbon on the Jellycon home screen."""
    # Fetch 16 most recently added scenes
    stash_data = stash_client.fetch_scenes({"sort": "created_at", "direction": "DESC"}, page=1, per_page=16)
    jellyfin_items = []
    for scene in stash_data.get("scenes", []):
        try:
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene))
        except Exception:
            pass
    return JSONResponse(jellyfin_items)