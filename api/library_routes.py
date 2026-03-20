import logging
import asyncio
import httpx
import datetime
import re
import hashlib
from starlette.responses import JSONResponse
from starlette.requests import Request
import config
from core import stash_client
from core import jellyfin_mapper
from core.jellyfin_mapper import encode_id, decode_id, hyphens

logger = logging.getLogger(__name__)

def _get_query_param(request: Request, param_name: str, default=None):
    """Safely extracts query parameters with case-insensitive matching."""
    values = []
    target_param = param_name.lower()
    
    for key, value in request.query_params.multi_items():
        if key.lower() == target_param:
            values.append(value)
            
    if values:
        return ",".join(values)
        
    return default

async def _get_libraries():
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    cache_version = getattr(config, "CACHE_VERSION", 0)
    
    def build_view(name, view_id):
        logo_hash = hashlib.md5(f"stash-logo-{cache_version}".encode()).hexdigest()
        
        return {
            "Name": name, "ServerId": server_id, "Id": view_id, "ItemId": view_id,
            "ChannelId": None, "IsFolder": True, 
            "Type": "UserView", # FIX 1: Enforce strict UserView type for Fladder
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": hyphens(view_id), "ItemId": view_id},
            "PrimaryImageAspectRatio": 1.7777777777777777, 
            "CollectionType": "movies",
            "LibraryOptions": {"PathInfos": []}, "Locations": [],
            "ImageTags": {"Primary": logo_hash, "Thumb": logo_hash}, 
            "HasPrimaryImage": True, 
            "HasThumb": True,
            "HasBackdrop": True,
            "BackdropImageTags": [logo_hash], 
            "ImageBlurHashes": {}, "LocationType": "FileSystem", "MediaType": "Unknown"
        }

    views = [
        build_view("Scenes (Everything)", encode_id("root", "scenes")),
        build_view("Scenes (Organized)", encode_id("root", "organized")),
        build_view("Scenes (Tagged)", encode_id("root", "tagged"))
    ]
    
    recent_days = getattr(config, "RECENT_DAYS", 14)
    if recent_days > 0:
        views.insert(1, build_view(f"Recently Added ({recent_days} Days)", encode_id("root", "recent")))
            
        
    if getattr(config, "ENABLE_FILTERS", True):
        views.append(build_view("Saved Filters", encode_id("root", "filters")))
            
    if getattr(config, "ENABLE_TAG_FILTERS", False):
        views.append(build_view("Tags", encode_id("root", "tags")))
    
    tag_names = getattr(config, "TAG_GROUPS", [])
    if tag_names:
        stash_base = config.get_stash_base()
        url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
        headers = {"Content-Type": "application/json"}
        if getattr(config, "STASH_API_KEY", ""):
            headers["ApiKey"] = config.STASH_API_KEY

        query = "query { allTags { id name } }"
        async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
            try:
                resp = await client.post(url, headers=headers, json={"query": query}, timeout=5.0)
                all_tags = resp.json().get("data", {}).get("allTags", [])
                
                for name in tag_names:
                    search_name = name.strip().lower()
                    match = next((t for t in all_tags if t['name'].strip().lower() == search_name), None)
                    if match:
                        view_id = encode_id("tag", str(match['id']))
                        views.append(build_view(match['name'], view_id))
            except Exception as e:
                logger.error(f"Failed to auto-resolve tag IDs: {e}")

    return views

async def endpoint_views(request: Request):
    views = await _get_libraries()
    return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})

async def endpoint_virtual_folders(request: Request):
    # FIX 2: Enforce strict VirtualFolderInfo schema so Fladder doesn't drop the array
    views = await _get_libraries() 
    virtual_folders = []
    
    for v in views:
        virtual_folders.append({
            "Name": v.get("Name"),
            "Locations": [],
            "CollectionType": v.get("CollectionType", "movies"),
            "LibraryOptions": {},
            "ItemId": v.get("Id"),
            "PrimaryImageItemId": v.get("Id"),
            "RefreshProgress": 0,
            "RefreshStatus": "Idle"
        })
        
    return JSONResponse(virtual_folders)

async def endpoint_items(request: Request):
    parent_id = _get_query_param(request, "ParentId")
    decoded_parent_id = decode_id(parent_id) if parent_id else None
    ids_param = _get_query_param(request, "Ids")
    
    try: start_index = int(_get_query_param(request, "StartIndex", 0))
    except: start_index = 0
        
    try: limit = int(_get_query_param(request, "Limit", getattr(config, "DEFAULT_PAGE_SIZE", 50)))
    except: limit = getattr(config, "DEFAULT_PAGE_SIZE", 50)

    search_term = _get_query_param(request, "SearchTerm")
    person_ids = (_get_query_param(request, "ArtistIds") or _get_query_param(request, "PeopleIds") or _get_query_param(request, "PersonIds"))
    tags_param = (_get_query_param(request, "Tags") or _get_query_param(request, "TagIds") or _get_query_param(request, "GenreIds"))
    filters_string = _get_query_param(request, "Filters", "")
    item_types = _get_query_param(request, "IncludeItemTypes", "").lower()
    recursive = _get_query_param(request, "Recursive", "false").lower() == "true"

    if not parent_id and not ids_param and not search_term and not recursive and not person_ids and not tags_param and not filters_string:
        if "movie" not in item_types and "episode" not in item_types:
            views = await _get_libraries()
            return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})

    if decoded_parent_id and decoded_parent_id.startswith("scene-"):
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

    if ids_param:
        raw_ids = []
        for i in ids_param.split(","):
            dec_i = decode_id(i)
            match = re.search(r'\d+', dec_i)
            if match: raw_ids.append(match.group())
                
        jellyfin_items = []
        stash_base = config.get_stash_base()
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
                except Exception: pass
                return None

        async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
            tasks = [fetch_single_scene(client, rid) for rid in raw_ids]
            results = await asyncio.gather(*tasks)
            
        for scene in results:
            if scene:
                try:
                    safe_root = encode_id("root", "scenes")
                    jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root))
                except Exception: pass
        return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": len(jellyfin_items), "StartIndex": 0})

    filter_args = {"sort": "created_at", "direction": "DESC"}
    scene_filter = {} 
    original_limit = limit

    if person_ids:
        raw_p_ids = []
        for p in person_ids.split(","):
            dec_p = decode_id(p)
            match = re.search(r'\d+', dec_p)
            if match: raw_p_ids.append(match.group())
        if raw_p_ids:
            scene_filter["performers"] = {"value": raw_p_ids, "modifier": "INCLUDES"}

    is_folder_override = False
    
    if decoded_parent_id:
        # --- FOLDER INTERCEPTS ---
        if decoded_parent_id in ["root-filters", "root-tags", "root-alltags"]:
            jellyfin_items = []
            server_id = getattr(config, "SERVER_ID", "stash-proxy")
            stash_base = config.get_stash_base()
            url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if getattr(config, "STASH_API_KEY", ""): headers["ApiKey"] = config.STASH_API_KEY
            
            async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
                if decoded_parent_id == "root-filters":
                    query = "query { findSavedFilters(mode: SCENES) { id name } }"
                    try:
                        resp = await client.post(url, headers=headers, json={"query": query}, timeout=10.0)
                        filters = resp.json().get("data", {}).get("findSavedFilters", [])
                        for f in filters:
                            jellyfin_items.append({
                                "Name": f.get("name"), "Id": encode_id("filter", str(f.get("id"))),
                                "Type": "CollectionFolder", "CollectionType": "movies", "IsFolder": True, "ServerId": server_id
                            })
                    except Exception as e: logger.error(f"Failed to fetch filters: {e}")
                    
                elif decoded_parent_id == "root-tags":
                    tag_names = getattr(config, "TAG_GROUPS", [])
                    if tag_names:
                        query = "query { allTags { id name } }"
                        try:
                            resp = await client.post(url, headers=headers, json={"query": query}, timeout=10.0)
                            all_tags = resp.json().get("data", {}).get("allTags", [])
                            for name in tag_names:
                                search_name = name.strip().lower()
                                match = next((t for t in all_tags if t['name'].strip().lower() == search_name), None)
                                if match:
                                    jellyfin_items.append({
                                        "Name": match['name'], "Id": encode_id("tag", str(match['id'])),
                                        "Type": "CollectionFolder", "CollectionType": "movies", "IsFolder": True, "ServerId": server_id
                                    })
                        except Exception as e: logger.error(f"Failed to fetch tags: {e}")
                        
                    if getattr(config, "ENABLE_ALL_TAGS", False):
                        jellyfin_items.append({
                            "Name": "All Tags", "Id": encode_id("root", "alltags"),
                            "Type": "CollectionFolder", "CollectionType": "movies", "IsFolder": True, "ServerId": server_id
                        })
                        
                elif decoded_parent_id == "root-alltags":
                    query = """query { findTags(filter: {per_page: -1, sort: "name", direction: ASC}) { tags { id name } } }"""
                    try:
                        resp = await client.post(url, headers=headers, json={"query": query}, timeout=15.0)
                        tags = resp.json().get("data", {}).get("findTags", {}).get("tags", [])
                        for t in tags:
                            jellyfin_items.append({
                                "Name": t.get("name"), "Id": encode_id("tag", str(t.get("id"))),
                                "Type": "CollectionFolder", "CollectionType": "movies", "IsFolder": True, "ServerId": server_id
                            })
                    except Exception as e: logger.error(f"Failed to fetch all tags: {e}")
                    
            total_record_count = len(jellyfin_items)
            if original_limit > 0 and decoded_parent_id == "root-alltags":
                jellyfin_items = jellyfin_items[start_index : start_index + original_limit]
                
            return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_record_count, "StartIndex": start_index})

        # --- STANDARD SCENE ROUTING ---
        if decoded_parent_id == "root-scenes":
            is_folder_override = True
        elif decoded_parent_id == "root-organized":
            scene_filter["organized"] = True
            is_folder_override = True
        elif decoded_parent_id == "root-tagged":
            scene_filter["tags"] = {"modifier": "NOT_NULL"}
            is_folder_override = True
        elif decoded_parent_id == "root-recent":
            recent_days = getattr(config, "RECENT_DAYS", 14)
            cutoff_date = (datetime.datetime.utcnow() - datetime.timedelta(days=recent_days)).strftime("%Y-%m-%dT%H:%M:%S")
            scene_filter["created_at"] = {"value": cutoff_date, "modifier": "GREATER_THAN"}
            is_folder_override = True
        elif decoded_parent_id.startswith("tag-"):
            raw_tag_id = decoded_parent_id.replace("tag-", "")
            scene_filter["tags"] = {"value": [raw_tag_id], "modifier": "INCLUDES"}
            is_folder_override = True
        elif decoded_parent_id.startswith("person-"):
            raw_person_id = decoded_parent_id.replace("person-", "")
            scene_filter["performers"] = {"value": [raw_person_id], "modifier": "INCLUDES"}
            is_folder_override = True
        elif decoded_parent_id.startswith("studio-"):
            raw_studio_id = decoded_parent_id.replace("studio-", "")
            scene_filter["studios"] = {"value": [raw_studio_id], "modifier": "INCLUDES"}
            is_folder_override = True
        elif decoded_parent_id.startswith("filter-"):
            raw_filter_id = decoded_parent_id.replace("filter-", "")
            is_folder_override = True
            
            stash_base = config.get_stash_base()
            url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
            headers = {"Content-Type": "application/json", "Accept": "application/json"}
            if getattr(config, "STASH_API_KEY", ""): headers["ApiKey"] = config.STASH_API_KEY
            
            # Fetch the saved filter to apply its custom rules
            query = """query FindSavedFilter($id: ID!) { findSavedFilter(id: $id) { filter find_filter object_filter } }"""
            async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
                try:
                    resp = await client.post(url, headers=headers, json={"query": query, "variables": {"id": raw_filter_id}}, timeout=5.0)
                    data = resp.json().get("data", {}).get("findSavedFilter", {})
                    
                    if data:
                        # Modern Stash (0.24+)
                        if data.get("object_filter"):
                            scene_filter.update(data["object_filter"])
                        if data.get("find_filter"):
                            if "q" in data["find_filter"]: filter_args["q"] = data["find_filter"]["q"]
                            if "sort" in data["find_filter"]: filter_args["sort"] = data["find_filter"]["sort"]
                            if "direction" in data["find_filter"]: filter_args["direction"] = data["find_filter"]["direction"]
                            
                        # Legacy Stash Fallback
                        elif data.get("filter"):
                            import json
                            parsed = json.loads(data["filter"])
                            if "scene_filter" in parsed:
                                scene_filter.update(parsed["scene_filter"])
                            if "q" in parsed: filter_args["q"] = parsed["q"]
                            if "sort" in parsed: filter_args["sort"] = parsed["sort"]
                            if "direction" in parsed: filter_args["direction"] = parsed["direction"]
                except Exception as e:
                    logger.error(f"Failed to apply saved filter {raw_filter_id}: {e}")

    if item_types:
        allowed = False
        for t in ["movie", "video", "series", "episode", "folder"]:
            if t in item_types:
                allowed = True
                break
        if not allowed:
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})

    sort_by = _get_query_param(request, "SortBy", "").lower()
    if "random" in sort_by: filter_args["sort"] = "random"
    elif "datecreated" in sort_by: filter_args["sort"] = "created_at"
    elif "dateplayed" in sort_by: filter_args["sort"] = "updated_at" 
    elif "name" in sort_by or "sortname" in sort_by: filter_args["sort"] = "title"
    if _get_query_param(request, "SortOrder", "").lower() == "ascending": filter_args["direction"] = "ASC"

    filter_list = [f.strip() for f in filters_string.split(",")] if filters_string else []
    is_favorite_param = _get_query_param(request, "isFavorite", "").lower()
    is_played_param = _get_query_param(request, "isPlayed", "").lower()
    
    if "IsFavorite" in filter_list or is_favorite_param == "true": 
        scene_filter["o_counter"] = {"value": 0, "modifier": "GREATER_THAN"}
    elif is_favorite_param == "false":
        scene_filter["o_counter"] = {"value": 0, "modifier": "EQUALS"}
        
    if "IsUnplayed" in filter_list or is_played_param == "false": 
        scene_filter["play_count"] = {"value": 0, "modifier": "EQUALS"}
    elif "IsPlayed" in filter_list or is_played_param == "true": 
        scene_filter["play_count"] = {"value": 0, "modifier": "GREATER_THAN"}

    if "IsResumable" in filter_list:
        filter_args["sort"] = "updated_at" 
        filter_args["direction"] = "DESC"
        limit = 100 

    years = _get_query_param(request, "Years")
    if years:
        y_l = []
        for y in years.split(","):
            dec_y = decode_id(y)
            match = re.search(r'\d{4}', dec_y)
            if match: y_l.append(int(match.group()))
        if y_l: scene_filter["date"] = {"value": f"{min(y_l)}-01-01", "value2": f"{max(y_l)}-12-31", "modifier": "BETWEEN"}
        
    if tags_param:
        raw_t = []
        for t in tags_param.split(","):
            dec_t = decode_id(t)
            match = re.search(r'\d+', dec_t)
            if match: raw_t.append(match.group())
        if raw_t: scene_filter["tags"] = {"value": raw_t, "modifier": "INCLUDES"}

    if search_term: filter_args["q"] = search_term

    if original_limit == 0 and not search_term and "IsResumable" not in filter_list:
        stash_data = stash_client.fetch_scenes(filter_args, page=1, per_page=1, scene_filter=scene_filter, ignore_sync_level=is_folder_override)
        if not stash_data:
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})
        return JSONResponse({"Items": [], "TotalRecordCount": stash_data.get("count", 0), "StartIndex": start_index})

    page = (start_index // limit) + 1 if limit > 0 else 1
    stash_data = stash_client.fetch_scenes(filter_args, page=page, per_page=limit, scene_filter=scene_filter, ignore_sync_level=is_folder_override)
    
    if not stash_data:
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})
    
    jellyfin_items = []
    for scene in stash_data.get("scenes", []):
        if search_term:
            title = scene.get("title") or scene.get("code") or ""
            if not title.lower().startswith(search_term.lower()): continue

        if "IsResumable" in filter_list:
            if not scene.get("resume_time") or scene.get("resume_time") <= 0: continue

        try:
            safe_root = encode_id("root", "scenes")
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root))
        except Exception: pass

    total_count = stash_data.get("count", 0)
    if search_term or "IsResumable" in filter_list:
        total_count = len(jellyfin_items)
        if original_limit > 0:
            jellyfin_items = jellyfin_items[start_index : start_index + original_limit]

    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_count, "StartIndex": start_index})

async def endpoint_item_details(request: Request):
    item_id = request.path_params.get("item_id", "")
    decoded_id = decode_id(item_id)
    cache_version = getattr(config, "CACHE_VERSION", 0)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    
    if decoded_id.startswith("root-") or decoded_id.startswith("tag-") or decoded_id.startswith("filter-"):
        if decoded_id.startswith("root-"): safe_id = encode_id("root", decoded_id.replace("root-", ""))
        elif decoded_id.startswith("tag-"): safe_id = encode_id("tag", decoded_id.replace("tag-", ""))
        elif decoded_id.startswith("filter-"): safe_id = encode_id("filter", decoded_id.replace("filter-", ""))
        
        return JSONResponse({
            "Name": "Folder", 
            "SortName": "Folder",
            "Id": safe_id, 
            "ServerId": server_id,
            "Type": "CollectionFolder",
            "CollectionType": "movies",
            "IsFolder": True
        })

    if decoded_id.startswith("studio-"):
        safe_id = encode_id("studio", decoded_id.replace("studio-", ""))
        return JSONResponse({
            "Name": "Studio", 
            "SortName": "Studio",
            "Id": safe_id, 
            "ServerId": server_id,
            "Type": "Studio", 
            "IsFolder": False
        })

    if decoded_id.startswith("person-"):
        raw_id = decoded_id.replace("person-", "")
        stash_base = config.get_stash_base()
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
                    
                    p_tag = hashlib.md5(f"person-{raw_id}-v{cache_version}".encode()).hexdigest()
                    perf_name = perf.get("name", "Unknown Person")
                    
                    return JSONResponse({
                        "Name": perf_name,
                        "SortName": perf_name,
                        "Id": item_id,
                        "ServerId": server_id,
                        "Type": "Person",
                        "IsFolder": False,
                        "ImageTags": {"Primary": p_tag} if perf.get("image_path") else {},
                        "HasPrimaryImage": bool(perf.get("image_path")),
                        
                        "MovieCount": 1,
                        "ChildCount": 1,
                        
                        "UserData": {
                            "PlaybackPositionTicks": 0,
                            "PlayCount": 0,
                            "IsFavorite": False,
                            "Played": False,
                            "Key": f"Person-{perf_name}",
                            "ItemId": item_id
                        }
                    })
            except Exception as e:
                logger.error(f"Failed to fetch performer details: {e}")
                
        return JSONResponse({
            "Name": "Person", 
            "SortName": "Person",
            "Id": item_id, 
            "ServerId": server_id,
            "Type": "Person", 
            "IsFolder": False
        })
        
    number_match = re.search(r'\d+', decoded_id)
    if not number_match:
        return JSONResponse({"error": f"Invalid ID format: {decoded_id}"}, status_code=400)
        
    raw_id = number_match.group()
    scene = stash_client.get_scene(raw_id)
    
    if scene:
        jellyfin_item = jellyfin_mapper.format_jellyfin_item(scene)
        return JSONResponse(jellyfin_item)
        
    return JSONResponse({"error": "Item not found"}, status_code=404)

async def endpoint_tags(request: Request):
    is_genre = "genre" in request.url.path.lower()
    item_type = "Genre" if is_genre else "Tag"

    stash_base = config.get_stash_base()
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

    jelly_tags = [{"Name": t.get("name"), "Id": encode_id("tag", str(t.get('id'))), "Type": item_type} for t in stash_tags]
    return JSONResponse({"Items": jelly_tags, "TotalRecordCount": len(jelly_tags), "StartIndex": 0})

async def endpoint_years(request: Request):
    current_year = datetime.datetime.now().year
    years = [{"Name": str(y), "Id": encode_id("year", str(y)), "Type": "Year", "ProductionYear": y} for y in range(current_year, 1989, -1)]
    return JSONResponse({"Items": years, "TotalRecordCount": len(years), "StartIndex": 0})

async def endpoint_studios(request: Request):
    studios = stash_client.get_all_studios()
    cache_version = getattr(config, "CACHE_VERSION", 0)
    
    jelly_studios = []
    for s in studios:
        s_tag = hashlib.md5(f"studio-{s.get('id')}-v{cache_version}".encode()).hexdigest()
        jelly_studios.append({
            "Name": s.get("name"), 
            "Id": encode_id("studio", str(s.get('id'))), 
            "Type": "Studio",
            "ImageTags": {"Primary": s_tag}, 
            "HasPrimaryImage": bool(s.get("image_path"))
        })
        
    return JSONResponse({"Items": jelly_studios, "TotalRecordCount": len(jelly_studios), "StartIndex": 0})

async def endpoint_empty_list(request: Request):
    return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

async def endpoint_empty_array(request: Request):
    """Provides a raw empty array for endpoints that do not use pagination."""
    return JSONResponse([])

async def endpoint_filters(request: Request):
    return JSONResponse({
        "Tags": [],
        "Genres": [],
        "Studios": [],
        "OfficialRatings": [],
        "Years": []
    })

async def endpoint_latest(request: Request):
    parent_id = _get_query_param(request, "ParentId")
    decoded_parent_id = decode_id(parent_id) if parent_id else None
    
    scene_filter = {}
    is_folder_override = False
    
    if decoded_parent_id:
        if decoded_parent_id == "root-scenes":
            is_folder_override = True
        elif decoded_parent_id == "root-organized":
            scene_filter["organized"] = True
            is_folder_override = True
        elif decoded_parent_id == "root-tagged":
            scene_filter["tags"] = {"modifier": "NOT_NULL"}
            is_folder_override = True
        elif decoded_parent_id == "root-recent":
            recent_days = getattr(config, "RECENT_DAYS", 14)
            cutoff_date = (datetime.datetime.utcnow() - datetime.timedelta(days=recent_days)).strftime("%Y-%m-%dT%H:%M:%S")
            scene_filter["created_at"] = {"value": cutoff_date, "modifier": "GREATER_THAN"}
            is_folder_override = True
        elif decoded_parent_id.startswith("tag-"):
            raw_tag_id = decoded_parent_id.replace("tag-", "")
            scene_filter["tags"] = {"value": [raw_tag_id], "modifier": "INCLUDES"}
            is_folder_override = True
            
    stash_data = stash_client.fetch_scenes(
        {"sort": "created_at", "direction": "DESC"}, 
        page=1, per_page=16, 
        scene_filter=scene_filter,
        ignore_sync_level=is_folder_override
    )
    
    jellyfin_items = []
    safe_root = encode_id("root", "scenes")
    
    for scene in stash_data.get("scenes", []):
        try:
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root))
        except Exception:
            pass
            
    return JSONResponse(jellyfin_items) 

async def endpoint_theme_songs(request: Request):
    """Satisfies strict Kotlin SDKs (Wholphin/Findroid) looking for Theme Songs."""
    item_id = request.path_params.get("item_id", "unknown")
    
    # The Kotlin SDK strictly requires the OwnerId field to be present
    return JSONResponse({
        "OwnerId": item_id,
        "Items": [],
        "TotalRecordCount": 0,
        "StartIndex": 0
    })

async def endpoint_special_features(request: Request):
    """Satisfies strict Kotlin SDKs looking for Special Features."""
    # The Kotlin SDK strictly expects a raw JSON Array, NOT an object containing an 'Items' array!
    return JSONResponse([])