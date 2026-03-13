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
    """Safely extracts query parameters, handling multiple values for the same key."""
    values = []
    # Use multi_items() to catch duplicate keys like ?type=Movie&type=Series
    for k, v in request.query_params.multi_items():
        if k.lower() == param_name.lower():
            values.append(v)
            
    if values:
        # Join multiple values with a comma (e.g., "Movie,Series")
        return ",".join(values)
        
    return default

async def endpoint_items(request: Request):
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

    # 1. ERSATZTV SPECIFIC FETCH
    if ids_param:
        raw_ids = [i.replace("scene-", "") for i in ids_param.split(",")]
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
                except Exception as e:
                    pass
        
        return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": len(jellyfin_items), "StartIndex": 0})

    # 2. NORMAL SEARCH & FILTER ENGINE
    filter_args = {"sort": "created_at", "direction": "DESC"}
    scene_filter = {} 
    original_limit = limit

    item_types = _get_query_param(request, "IncludeItemTypes", "").lower()
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

    filters_string = _get_query_param(request, "Filters", "")
    filter_list = [f.strip() for f in filters_string.split(",")] 

    if "IsUnplayed" in filter_list:
        scene_filter["play_count"] = {"value": 0, "modifier": "EQUALS"}
    elif "IsPlayed" in filter_list:
        scene_filter["play_count"] = {"value": 0, "modifier": "GREATER_THAN"}
        
    if "IsFavorite" in filter_list:
        scene_filter["o_counter"] = {"value": 0, "modifier": "GREATER_THAN"}
        
    # In Progress - Use memory filtering because Stash doesn't index resume_time natively!
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

    tags_param = _get_query_param(request, "Tags") or _get_query_param(request, "TagIds") or _get_query_param(request, "GenreIds")
    if tags_param:
        raw_tags = [t.replace("tag-", "") for t in tags_param.split(",") if t.replace("tag-", "").isdigit()]
        if raw_tags:
            scene_filter["tags"] = {"value": raw_tags, "modifier": "INCLUDES"}

    name_starts = _get_query_param(request, "NameStartsWith")
    search_term = _get_query_param(request, "SearchTerm")
    if name_starts:
        filter_args["q"] = name_starts
        filter_args["sort"] = "title"
        filter_args["direction"] = "ASC"
        limit = -1 
    elif search_term:
        filter_args["q"] = search_term

    if original_limit == 0 and not name_starts and "IsResumable" not in filter_list:
        stash_data = stash_client.fetch_scenes(filter_args, page=1, per_page=1, scene_filter=scene_filter)
        return JSONResponse({"Items": [], "TotalRecordCount": stash_data.get("count", 0), "StartIndex": start_index})

    page = (start_index // limit) + 1 if limit > 0 else 1
    stash_data = stash_client.fetch_scenes(filter_args, page=page, per_page=limit, scene_filter=scene_filter)
    
    jellyfin_items = []
    for scene in stash_data.get("scenes", []):
        if name_starts:
            title = scene.get("title") or scene.get("code") or ""
            if not title.lower().startswith(name_starts.lower()):
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
    if name_starts or "IsResumable" in filter_list:
        total_count = len(jellyfin_items)
        if original_limit > 0:
            jellyfin_items = jellyfin_items[start_index : start_index + original_limit]

    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_count, "StartIndex": start_index})

def _get_libraries():
    server_id = getattr(config, "SERVER_ID", "")
    root_id = encode_id("root", "scenes")
    
    # Helper to insert standard UUID hyphens for the UserData Key
    def hyphens(h):
        if len(h) != 32: return h
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    
    # Base template matching the real Jellyfin Server response EXACTLY
    def build_view(name, view_id):
        return {
            "Name": name,
            "ServerId": server_id,
            "Id": view_id,
            "ChannelId": None,
            "IsFolder": True,
            "Type": "CollectionFolder",
            "UserData": {
                "PlaybackPositionTicks": 0,
                "PlayCount": 0,
                "IsFavorite": False,
                "Played": False,
                "Key": hyphens(view_id),
                "ItemId": view_id
            },
            "PrimaryImageAspectRatio": 1.7777777777777777,
            "CollectionType": "movies",
            "ImageTags": {},
            "BackdropImageTags": [],
            "ImageBlurHashes": {},
            "LocationType": "FileSystem",
            "MediaType": "Unknown"
        }

    views = [build_view("Scenes", root_id)]
    
    for tag in getattr(config, "TAG_GROUPS", []):
        if tag.strip():
            t = tag.strip()
            tag_id = encode_id("tag", t)
            views.append(build_view(t, tag_id))
            
    return views

async def endpoint_views(request: Request):
    views = _get_libraries()
    return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})

async def endpoint_virtual_folders(request: Request):
    return JSONResponse(content=_get_libraries(), headers={"Content-Type": "application/json; charset=utf-8"})

import re

async def endpoint_item_details(request: Request):
    item_id = request.path_params.get("item_id", "")
    decoded_id = decode_id(item_id)
    
    if decoded_id == "root-scenes" or decoded_id.startswith("tag-"):
        safe_id = encode_id("root", "scenes") if decoded_id == "root-scenes" else encode_id("tag", decoded_id.replace("tag-", ""))
        return JSONResponse({"Name": "Folder", "Id": safe_id, "Type": "CollectionFolder", "IsFolder": True})
        
    # Extract ONLY the numbers for the Stash query
    number_match = re.search(r'\d+', decoded_id)
    if not number_match:
        return JSONResponse({"error": f"Invalid ID format: {decoded_id}"}, status_code=400)
        
    raw_id = number_match.group()
    
    # Now query Stash with a guaranteed pure number
    stash_data = stash_client.fetch_scenes({"id": int(raw_id)}, page=1, per_page=1)
    
    if stash_data and stash_data.get("scenes"):
        scene = stash_data["scenes"][0]
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
    stash_data = stash_client.fetch_scenes({"sort": "created_at", "direction": "DESC"}, page=1, per_page=16)
    jellyfin_items = []
    
    # Generate the safe UUID for the parent folder
    safe_root = encode_id("root", "scenes")
    
    for scene in stash_data.get("scenes", []):
        try:
            # Explicitly pass the safe_root so it NEVER defaults to the raw string
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=safe_root))
        except Exception:
            pass
            
    return JSONResponse(jellyfin_items)