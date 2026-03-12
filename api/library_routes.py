import logging
import asyncio
import httpx
import datetime
from starlette.responses import JSONResponse
from starlette.requests import Request
import config
from core import stash_client
from core import jellyfin_mapper

logger = logging.getLogger(__name__)

def _get_query_param(request: Request, param_name: str, default=None):
    for k, v in request.query_params.items():
        if k.lower() == param_name.lower():
            return v
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
                    jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or "root-scenes"))
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
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or "root-scenes"))
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
    views = [{
        "Name": "Scenes", "Id": "root-scenes", "Guid": "root-scenes", 
        "ServerId": server_id, "CollectionType": "movies", "Type": "CollectionFolder", 
        "ItemId": "root-scenes", "LibraryOptions": {"PathInfos": []}, "Locations": []
    }]
    for tag in getattr(config, "TAG_GROUPS", []):
        if tag.strip():
            t = tag.strip()
            views.append({
                "Name": t, "Id": f"tag-{t}", "Guid": f"tag-{t}", 
                "ServerId": server_id, "CollectionType": "movies", "Type": "CollectionFolder", 
                "ItemId": f"tag-{t}", "LibraryOptions": {"PathInfos": []}, "Locations": []
            })
    return views

async def endpoint_views(request: Request):
    views = _get_libraries()
    return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})

async def endpoint_virtual_folders(request: Request):
    return JSONResponse(content=_get_libraries(), headers={"Content-Type": "application/json; charset=utf-8"})

async def endpoint_item_details(request: Request):
    item_id = request.path_params.get("item_id", "")
    if item_id == "root-scenes" or item_id.startswith("tag-"):
        return JSONResponse({"Name": "Folder", "Id": item_id, "Type": "CollectionFolder", "IsFolder": True})
        
    raw_id = item_id.replace("scene-", "")
    if not raw_id.isdigit():
        return JSONResponse({"error": "Item not found"}, status_code=404)
    
    scene = stash_client.get_scene(raw_id)
    if not scene:
        return JSONResponse({"error": "Item not found"}, status_code=404)
        
    return JSONResponse(jellyfin_mapper.format_jellyfin_item(scene))

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
    for scene in stash_data.get("scenes", []):
        try:
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene))
        except Exception:
            pass
    return JSONResponse(jellyfin_items)