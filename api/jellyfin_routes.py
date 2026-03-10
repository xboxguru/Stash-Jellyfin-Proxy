import logging
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.responses import Response
import config
from core import stash_client
from core import jellyfin_mapper
import asyncio
import httpx

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

    # If Limit=0, ErsatzTV just wants the TotalRecordCount, not the actual items!
    if limit == 0:
        stash_data = stash_client.fetch_scenes(filter_args, page=1, per_page=1)
        return JSONResponse({
            "Items": [],
            "TotalRecordCount": stash_data.get("count", 0),
            "StartIndex": start_index
        })

    # Calculate pagination
    page = (start_index // limit) + 1 if limit > 0 else 1
    stash_data = stash_client.fetch_scenes(filter_args, page=page, per_page=limit)
    
    jellyfin_items = []
    for scene in stash_data.get("scenes", []):
        try:
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or "root-scenes"))
        except Exception as e:
            logger.error(f"Failed to map scene during pagination: {e}")

    return JSONResponse({
        "Items": jellyfin_items,
        "TotalRecordCount": stash_data.get("count", 0),
        "StartIndex": start_index
    })

def _get_libraries():
    """Matches the exact 'VirtualFolder' schema."""
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    
    views = [
        {
            "Name": "Scenes",
            "Id": "root-scenes",
            "Guid": "root-scenes",
            "ServerId": server_id,
            "CollectionType": "movies",
            "Type": "CollectionFolder",
            "ItemId": "root-scenes"
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
                "ItemId": f"tag-{t}"
            })
            
    return views

async def endpoint_views(request: Request):
    """
    Original logic: Return raw array for BOTH Views and VirtualFolders.
    This fixed 'Deserialization' errors in strict C# clients.
    """
    return JSONResponse(content=_get_libraries())

async def endpoint_virtual_folders(request: Request):
    """Returns libraries as a raw Array with strict header enforcement."""
    views = _get_libraries()
    return JSONResponse(
        content=views, 
        headers={"Content-Type": "application/json; charset=utf-8"}
    )

async def endpoint_item_details(request: Request):
    """
    Handles requests for a single specific item.
    ErsatzTV calls this when you do a 'MediaInfo' scan on a file.
    """
    item_id = request.path_params.get("item_id", "")
    
    # Strip the "scene-" prefix we added in the mapper
    raw_id = item_id.replace("scene-", "")
    
    # Fetch the single scene from Stash
    scene = stash_client.get_scene(raw_id)
    
    if not scene:
        return JSONResponse({"error": "Item not found"}, status_code=404)
        
    # Format and return it
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

async def endpoint_system_info_public(request: Request):
    """Public handshake endpoint required by Jellyfin clients.""" 
    # Dynamically grab the IP and Port ErsatzTV used to reach us
    client_host = request.headers.get("Host", f"127.0.0.1:{config.PROXY_PORT}")
    
    return JSONResponse({
        "LocalAddress": f"http://{client_host}",
        "ServerName": getattr(config, "SERVER_NAME", "Stash Media Server"),
        "Version": "10.8.13",
        "OperatingSystem": "Linux",
        "Id": getattr(config, "SERVER_ID", "stash-proxy")
    })

async def endpoint_sessions_playing(request: Request):
    """Receives playback start and progress reports from Jellyfin clients."""
    import state
    import time
    
    try:
        data = await request.json()
        item = data.get("Item", {})
        session_id = data.get("PlaySessionId", "unknown_session")
        
        # Ensure the list exists
        if not hasattr(state, "active_streams"):
            state.active_streams = []
            
        # Check if we are already tracking this stream
        existing_stream = next((s for s in state.active_streams if s.get("id") == session_id), None)
        
        if not existing_stream:
            # 1. Add it to the Active Streams dashboard
            title = item.get("Name", "Unknown Scene")
            stream_info = {
                "id": session_id,
                "title": title,
                "performer": "", # We can leave this blank or fetch it later
                "user": "Proxy User", 
                "clientIp": request.client.host if request.client else "Unknown",
                "clientType": data.get("ClientName", "ErsatzTV / Infuse"),
                "started": int(time.time())
            }
            state.active_streams.append(stream_info)
            
            # 2. Increment Proxy Usage Stats
            state.stats["streams_today"] += 1
            state.stats["total_streams"] += 1
            
            # 3. Track Top Played Scene
            scene_id = item.get("Id", "unknown")
            if scene_id not in state.stats["top_played"]:
                state.stats["top_played"][scene_id] = {"title": title, "performer": "Unknown", "count": 0}
            state.stats["top_played"][scene_id]["count"] += 1

    except Exception as e:
        logger.error(f"Error parsing playing session: {e}")
        
    # Always return 204 No Content for playback reporting so the client is happy
    return JSONResponse({}, status_code=204)

async def endpoint_sessions_stopped(request: Request):
    """Receives playback stopped reports to clear active streams."""
    import state
    try:
        data = await request.json()
        session_id = data.get("PlaySessionId", "unknown_session")
        
        if hasattr(state, "active_streams"):
            # Remove the stream with this session ID from the memory bank
            state.active_streams = [s for s in state.active_streams if s.get("id") != session_id]
            
    except Exception as e:
        logger.error(f"Error removing stopped session: {e}")

    return JSONResponse({}, status_code=204)

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
    fake_user = {
        "Name": expected_user or "admin",
        "ServerId": getattr(config, "SERVER_ID", "stash-proxy"),
        "Id": "stash-user-id",
        "HasPassword": bool(expected_pass),
        "Policy": {"IsAdministrator": True}
    }
    
    return JSONResponse({
        "User": fake_user,
        "SessionInfo": {
            "UserId": "stash-user-id",
            "Id": "stash-session-id"
        },
        "AccessToken": config.PROXY_API_KEY,
        "ServerId": getattr(config, "SERVER_ID", "stash-proxy")
    })

async def endpoint_users(request: Request):
    """Returns a fake user list containing our single proxy user."""
    user = {
        "Name": getattr(config, "SJS_USER", "admin") or "admin",
        "ServerId": getattr(config, "SERVER_ID", "stash-proxy"),
        "Id": "stash-user-id",
        "Policy": {"IsAdministrator": True}
    }
    return JSONResponse([user])

async def endpoint_user(request: Request):
    """Returns details for our specific fake user."""
    return JSONResponse({
        "Name": getattr(config, "SJS_USER", "admin") or "admin",
        "ServerId": getattr(config, "SERVER_ID", "stash-proxy"),
        "Id": "stash-user-id",
        "Policy": {"IsAdministrator": True}
    })

async def endpoint_system_info(request: Request):
    """Authenticated system info request."""
    client_host = request.headers.get("Host", f"127.0.0.1:{config.PROXY_PORT}")
    
    return JSONResponse({
        "LocalAddress": f"http://{client_host}",
        "ServerName": getattr(config, "SERVER_NAME", "Stash Media Server"),
        "Version": "10.8.13",
        "OperatingSystem": "Linux",
        "Id": getattr(config, "SERVER_ID", "stash-proxy")
    })

async def endpoint_quickconnect_enabled(request: Request):
    """Tells clients that QuickConnect is disabled, forcing standard login."""
    return JSONResponse(False)

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