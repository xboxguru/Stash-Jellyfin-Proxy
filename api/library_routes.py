import logging
import asyncio
import httpx
import datetime
import re
from starlette.responses import JSONResponse
from starlette.requests import Request
import config
from core import stash_client
from core import jellyfin_mapper
from core.jellyfin_mapper import encode_id, decode_id

logger = logging.getLogger(__name__)

def _get_query_param(request: Request, param_name: str, default=None):
    """Safely extracts query parameters with case-insensitive matching."""
    values = []
    
    # Query parameters are case-sensitive in HTTP. We must check against lower()
    target_param = param_name.lower()
    
    # Iterate through all raw query keys provided by the client
    for key, value in request.query_params.multi_items():
        if key.lower() == target_param:
            values.append(value)
            
    if values:
        return ",".join(values)
        
    return default

def _get_libraries():
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    root_id = encode_id("root", "scenes")
    
    def hyphens(h):
        if len(h) != 32: return h
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    
    views = [{
        "Name": "Scenes",
        "ServerId": server_id,
        "Id": root_id,
        "ItemId": root_id,
        "ChannelId": None,
        "IsFolder": True,
        "Type": "CollectionFolder",
        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": 0,
            "IsFavorite": False,
            "Played": False,
            "Key": hyphens(root_id),
            "ItemId": root_id
        },
        "PrimaryImageAspectRatio": 1.7777777777777777,
        "CollectionType": "movies",
        
        # --- TUNARR STRICT SCHEMA MOCKS ---
        "LibraryOptions": {
            "PathInfos": []  # <-- ADDED FOR TUNARR
        },    
        "Locations": [],     

        "ImageTags": {"Primary": "stash-logo-1"}, 
        "HasPrimaryImage": True, 
        
        "BackdropImageTags": [],
        "ImageBlurHashes": {},
        "LocationType": "FileSystem",
        "MediaType": "Unknown"
    }]
    return views

async def endpoint_items(request: Request):
    parent_id = _get_query_param(request, "ParentId")
    decoded_parent_id = decode_id(parent_id) if parent_id else None
    ids_param = _get_query_param(request, "Ids")
    
    try:
        start_index = int(_get_query_param(request, "StartIndex", 0))
    except:
        start_index = 0
        
    try:
        limit = int(_get_query_param(request, "Limit", getattr(config, "DEFAULT_PAGE_SIZE", 50)))
    except:
        limit = getattr(config, "DEFAULT_PAGE_SIZE", 50)

    # ==========================================
    # FINDROID FIX 1: ROOT DIRECTORY HIERARCHY
    # ==========================================
    # This stops Findroid from treating your videos as Library Folders.
    search_term = _get_query_param(request, "SearchTerm")
    person_ids = (_get_query_param(request, "ArtistIds") or _get_query_param(request, "PeopleIds") or _get_query_param(request, "PersonIds"))
    tags_param = (_get_query_param(request, "Tags") or _get_query_param(request, "TagIds") or _get_query_param(request, "GenreIds"))
    filters_string = _get_query_param(request, "Filters", "")
    item_types = _get_query_param(request, "IncludeItemTypes", "").lower()
    recursive = _get_query_param(request, "Recursive", "false").lower() == "true"

    if not parent_id and not ids_param and not search_term and not recursive and not person_ids and not tags_param and not filters_string:
        if "movie" not in item_types and "episode" not in item_types:
            views = _get_libraries()
            return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})

    # ==========================================
    # FINDROID FIX 2: STRICT LEAF NODE TERMINATION
    # ==========================================
    if decoded_parent_id and ("scene-" in decoded_parent_id or "person-" in decoded_parent_id):
        return JSONResponse({
            "Items": [], 
            "TotalRecordCount": 0, 
            "StartIndex": 0
        })

    # 1. ERSATZTV SPECIFIC FETCH
    # 1. ERSATZTV / TUNARR SPECIFIC FETCH
    if ids_param:
        raw_ids = []
        for i in ids_param.split(","):
            # Properly decode the hex ID back into a raw Stash number
            dec_i = decode_id(i)
            match = re.search(r'\d+', dec_i)
            if match:
                raw_ids.append(match.group())
                
        jellyfin_items = []
        stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
        url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if getattr(config, "STASH_API_KEY", ""):
            headers["ApiKey"] = config.STASH_API_KEY
        
        semaphore = asyncio.Semaphore(10)
        async def fetch_single_scene(client, raw_id):
            async with semaphore:
                query = f"query FindScene($id: ID!) {{ findScene(id: $id) {{ {stash_client.SCENE_FIELDS} }} }}"
                try:
                    resp = await client.post(url, headers=headers, json={"query": query, "variables": {"id": raw_id}}, timeout=10.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data and "data" in data and data["data"].get("findScene"):
                            return data["data"]["findScene"]
                except Exception as e:
                    logger.error(f"Concurrent fetch failed for scene {raw_id}: {e}")
                return None

        async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
            tasks = [fetch_single_scene(client, rid) for rid in raw_ids]
            results = await asyncio.gather(*tasks)
            
        for scene in results:
            if scene:
                try:
                    safe_root = encode_id("root", "scenes")
                    jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root))
                except Exception:
                    pass
        
        return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": len(jellyfin_items), "StartIndex": 0})

    # 2. NORMAL SEARCH & FILTER ENGINE
    filter_args = {"sort": "created_at", "direction": "DESC"}
    scene_filter = {} 
    original_limit = limit

    if person_ids:
        raw_p_ids = []
        for p in person_ids.split(","):
            dec_p = decode_id(p)
            match = re.search(r'\d+', dec_p)
            if match: 
                raw_p_ids.append(match.group())
        if raw_p_ids:
            scene_filter["performers"] = {"value": raw_p_ids, "modifier": "INCLUDES"}

    if decoded_parent_id and "tag-" in decoded_parent_id:
        tag_id_match = re.search(r'\d+', decoded_parent_id)
        if tag_id_match:
            scene_filter["tags"] = {"value": [tag_id_match.group()], "modifier": "INCLUDES"}

    if item_types:
        allowed = False
        for t in ["movie", "video", "series", "episode"]:
            if t in item_types:
                allowed = True
                break
        if not allowed:
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})

    sort_by = _get_query_param(request, "SortBy", "").lower()
    if "random" in sort_by:
        filter_args["sort"] = "random"
    elif "datecreated" in sort_by:
        filter_args["sort"] = "created_at"
    elif "dateplayed" in sort_by:
        filter_args["sort"] = "updated_at" 
    elif "name" in sort_by or "sortname" in sort_by:
        filter_args["sort"] = "title"
        
    sort_order = _get_query_param(request, "SortOrder", "").lower()
    if sort_order == "ascending":
        filter_args["direction"] = "ASC"

    filter_list = [f.strip() for f in filters_string.split(",")] if filters_string else []

    if "IsUnplayed" in filter_list:
        scene_filter["play_count"] = {"value": 0, "modifier": "EQUALS"}
    elif "IsPlayed" in filter_list:
        scene_filter["play_count"] = {"value": 0, "modifier": "GREATER_THAN"}
        
    if "IsFavorite" in filter_list:
        scene_filter["o_counter"] = {"value": 0, "modifier": "GREATER_THAN"}
        
    if "IsResumable" in filter_list:
        filter_args["sort"] = "updated_at" 
        filter_args["direction"] = "DESC"
        limit = -1 

    years = _get_query_param(request, "Years")
    if years:
        year_list = [int(y) for y in years.split(",") if y.isdigit()]
        if year_list:
            min_year = min(year_list)
            max_year = max(year_list)
            scene_filter["date"] = {"value": f"{min_year}-01-01", "value2": f"{max_year}-12-31", "modifier": "BETWEEN"}

    if tags_param:
        raw_tags = [t.replace("tag-", "") for t in tags_param.split(",") if t.replace("tag-", "").isdigit()]
        if raw_tags:
            scene_filter["tags"] = {"value": raw_tags, "modifier": "INCLUDES"}

    if search_term:
        filter_args["q"] = search_term

    if original_limit == 0 and not search_term and "IsResumable" not in filter_list:
        stash_data = stash_client.fetch_scenes(filter_args, page=1, per_page=1, scene_filter=scene_filter)
        if not stash_data:
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})
        return JSONResponse({"Items": [], "TotalRecordCount": stash_data.get("count", 0), "StartIndex": start_index})

    page = (start_index // limit) + 1 if limit > 0 else 1
    stash_data = stash_client.fetch_scenes(filter_args, page=page, per_page=limit, scene_filter=scene_filter)
    
    if not stash_data:
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})
    
    jellyfin_items = []
    for scene in stash_data.get("scenes", []):
        if search_term:
            title = scene.get("title") or scene.get("code") or ""
            if not title.lower().startswith(search_term.lower()):
                continue

        if "IsResumable" in filter_list:
            if not scene.get("resume_time") or scene.get("resume_time") <= 0:
                continue

        try:
            safe_root = encode_id("root", "scenes")
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root))
        except Exception:
            pass

    total_count = stash_data.get("count", 0)
    if search_term or "IsResumable" in filter_list:
        total_count = len(jellyfin_items)
        if original_limit > 0:
            jellyfin_items = jellyfin_items[start_index : start_index + original_limit]

    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_count, "StartIndex": start_index})

async def endpoint_views(request: Request):
    views = _get_libraries()
    return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})

async def endpoint_virtual_folders(request: Request):
    return JSONResponse(content=_get_libraries(), headers={"Content-Type": "application/json; charset=utf-8"})

async def endpoint_item_details(request: Request):
    item_id = request.path_params.get("item_id", "")
    decoded_id = decode_id(item_id)
    
    # 1. Catch Root Folders and Tags
    if decoded_id == "root-scenes" or decoded_id.startswith("tag-"):
        safe_id = encode_id("root", "scenes") if decoded_id == "root-scenes" else encode_id("tag", decoded_id.replace("tag-", ""))
        return JSONResponse({"Name": "Folder", "Id": safe_id, "Type": "CollectionFolder", "IsFolder": True})

    # 2. Catch Studios
    if decoded_id.startswith("studio-"):
        safe_id = encode_id("studio", decoded_id.replace("studio-", ""))
        return JSONResponse({"Name": "Studio", "Id": safe_id, "Type": "Studio", "IsFolder": True})

    # 3. Catch Performers (Actors)
    if decoded_id.startswith("person-"):
        raw_id = decoded_id.replace("person-", "")
        stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
        url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if getattr(config, "STASH_API_KEY", ""):
            headers["ApiKey"] = config.STASH_API_KEY
            
        query = """query FindPerformer($id: ID!) { findPerformer(id: $id) { id name image_path } }"""
        async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
            try:
                resp = await client.post(url, headers=headers, json={"query": query, "variables": {"id": raw_id}}, timeout=10.0)
                data = resp.json()
                if data and "data" in data and data["data"].get("findPerformer"):
                    perf = data["data"]["findPerformer"]
                    return JSONResponse({
                        "Name": perf.get("name", "Unknown Person"),
                        "Id": item_id,
                        "Type": "Person",
                        "IsFolder": True, # Required so clicking them lists their movies
                        "ImageTags": {"Primary": "primary"} if perf.get("image_path") else {},
                        "HasPrimaryImage": bool(perf.get("image_path"))
                    })
            except Exception as e:
                logger.error(f"Failed to fetch performer details: {e}")
                
        # Fallback if Stash is unreachable
        return JSONResponse({"Name": "Person", "Id": item_id, "Type": "Person", "IsFolder": True})
        
    # 4. Finally, if it's none of the above, it MUST be a Scene.
    number_match = re.search(r'\d+', decoded_id)
    if not number_match:
        return JSONResponse({"error": f"Invalid ID format: {decoded_id}"}, status_code=400)
        
    raw_id = number_match.group()
    
    # --- FINDROID FIX: AVOID 422 ERRRORS ---
    # Use get_scene (singular) instead of fetch_scenes
    scene = stash_client.get_scene(raw_id)
    
    if scene:
        jellyfin_item = jellyfin_mapper.format_jellyfin_item(scene)
        return JSONResponse(jellyfin_item)
        
    return JSONResponse({"error": "Item not found"}, status_code=404)

async def endpoint_tags(request: Request):
    is_genre = "genre" in request.url.path.lower()
    item_type = "Genre" if is_genre else "Tag"

    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
    url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if getattr(config, "STASH_API_KEY", ""):
        headers["ApiKey"] = config.STASH_API_KEY
        
    query = """query { findTags(filter: {per_page: -1, sort: "name", direction: ASC}) { tags { id name } } }"""
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            resp = await client.post(url, headers=headers, json={"query": query}, timeout=10.0)
            stash_tags = resp.json().get("data", {}).get("findTags", {}).get("tags", [])
        except Exception:
            stash_tags = []

    jelly_tags = [{"Name": t.get("name"), "Id": f"tag-{t.get('id')}", "Type": item_type} for t in stash_tags]
    return JSONResponse({"Items": jelly_tags, "TotalRecordCount": len(jelly_tags), "StartIndex": 0})

async def endpoint_years(request: Request):
    current_year = datetime.datetime.now().year
    years = [{"Name": str(y), "Id": str(y), "Type": "Year", "ProductionYear": y} for y in range(current_year, 1989, -1)]
    return JSONResponse({"Items": years, "TotalRecordCount": len(years), "StartIndex": 0})

async def endpoint_studios(request: Request):
    studios = stash_client.get_all_studios()
    img_version = getattr(config, "IMAGE_VERSION", 0)
    jelly_studios = [{
        "Name": s.get("name"), "Id": f"studio-{s.get('id')}", "Type": "Studio",
        "ImageTags": {"Primary": f"s-{s.get('id')}-v{img_version}"}, "HasPrimaryImage": bool(s.get("image_path"))
    } for s in studios]
    return JSONResponse({"Items": jelly_studios, "TotalRecordCount": len(jelly_studios), "StartIndex": 0})

async def endpoint_empty_list(request: Request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_latest(request: Request):
    parent_id = _get_query_param(request, "ParentId")
    
    stash_data = stash_client.fetch_scenes({"sort": "created_at", "direction": "DESC"}, page=1, per_page=16)
    jellyfin_items = []
    
    safe_root = encode_id("root", "scenes")
    
    for scene in stash_data.get("scenes", []):
        try:
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root))
        except Exception:
            pass
            
    # Note: Latest returns an array directly, NOT an object with 'Items'
    return JSONResponse(jellyfin_items)