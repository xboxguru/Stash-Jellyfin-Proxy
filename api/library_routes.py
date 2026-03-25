import logging
import asyncio
import datetime
import re
import hashlib
from starlette.responses import JSONResponse
from starlette.requests import Request
import config
from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import encode_id, decode_id, hyphens

logger = logging.getLogger(__name__)

def _get_query_param(request: Request, param_name: str, default=None):
    """Safely extracts query parameters with case-insensitive matching."""
    values = [value for key, value in request.query_params.multi_items() if key.lower() == param_name.lower()]
    return ",".join(values) if values else default

def _transform_saved_filter(object_filter):
    """Translates Stash UI filter definitions into Stash Backend GraphQL definitions."""
    if not object_filter or not isinstance(object_filter, dict): return {}
    result = {}
    for key, value in object_filter.items():
        if value is None: continue
        
        if key in ('AND', 'OR', 'NOT'):
            if isinstance(value, list): result[key] = [_transform_saved_filter(v) for v in value if v]
            elif isinstance(value, dict): result[key] = _transform_saved_filter(value)
            continue
            
        if isinstance(value, dict):
            modifier = value.get('modifier')
            val = value.get('value')
            
            if 'items' in value:
                ids = [item.get('id') for item in value['items'] if isinstance(item, dict) and item.get('id')]
                excludes = [e.get('id') if isinstance(e, dict) else e for e in value.get('excluded', [])]
                result[key] = {'value': ids, 'modifier': modifier, 'depth': value.get('depth', 0), 'excludes': excludes}
                continue
                
            if modifier in ('IS_NULL', 'NOT_NULL'):
                result[key] = {'value': '', 'modifier': modifier}
                continue
                
            if isinstance(val, dict) and 'value' in val: val = val['value']
                
            if modifier and val is not None:
                transformed = {'modifier': modifier, 'value': val}
                for k, v in value.items():
                    if k not in ('modifier', 'value'): transformed[k] = v
                result[key] = transformed
                continue
        result[key] = value
    return result

async def _get_libraries():
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    cache_version = getattr(config, "CACHE_VERSION", 0)
    
    def build_view(name, view_id, is_standard_folder=False):
        logo_hash = hashlib.md5(f"stash-logo-{cache_version}".encode()).hexdigest()
        return {
            "Name": name, "ServerId": server_id, "Id": view_id, "ItemId": view_id,
            "ChannelId": None, "IsFolder": True, "Type": "Folder" if is_standard_folder else "UserView", 
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": hyphens(view_id), "ItemId": view_id},
            "PrimaryImageAspectRatio": 1.7777777777777777, "CollectionType": "folders" if is_standard_folder else "movies",
            "LibraryOptions": {"PathInfos": []}, "Locations": [],
            "ImageTags": {"Primary": logo_hash, "Thumb": logo_hash}, "HasPrimaryImage": True, "HasThumb": True, "HasBackdrop": True,
            "BackdropImageTags": [logo_hash], "ImageBlurHashes": {}, "LocationType": "FileSystem", "MediaType": "Unknown"
        }

    views = [
        build_view("Scenes (Everything)", encode_id("root", "scenes")),
        build_view("Scenes (Organized)", encode_id("root", "organized")),
        build_view("Scenes (Tagged)", encode_id("root", "tagged"))
    ]
    
    recent_days = getattr(config, "RECENT_DAYS", 14)
    if recent_days > 0: views.insert(1, build_view(f"Recently Added ({recent_days} Days)", encode_id("root", "recent")))
    if getattr(config, "ENABLE_FILTERS", True): views.append(build_view("Saved Filters", encode_id("root", "filters"), is_standard_folder=True))
    if getattr(config, "ENABLE_TAG_FILTERS", False): views.append(build_view("Stash Tags", encode_id("root", "stashtags"), is_standard_folder=True))
    
    tag_names = getattr(config, "TAG_GROUPS", [])
    if tag_names:
        all_tags = await stash_client.get_all_tags()
        for name in tag_names:
            search_name = name.strip().lower()
            match = next((t for t in all_tags if t['name'].strip().lower() == search_name), None)
            if match: views.append(build_view(match['name'], encode_id("tag", str(match['id'])), is_standard_folder=True))

    return views

async def endpoint_views(request: Request):
    views = await _get_libraries()
    return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})

async def endpoint_virtual_folders(request: Request):
    views = await _get_libraries() 
    virtual_folders = [{"Name": v.get("Name"), "Locations": [], "CollectionType": v.get("CollectionType", "movies"), "LibraryOptions": {}, "ItemId": v.get("Id"), "PrimaryImageItemId": v.get("Id"), "RefreshProgress": 0, "RefreshStatus": "Idle"} for v in views]
    return JSONResponse(virtual_folders)

async def endpoint_items(request: Request):
    """Core routing engine for filtering, searching, and paginating library items."""
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
    studio_ids_param = _get_query_param(request, "StudioIds")
    filters_string = _get_query_param(request, "Filters", "")
    item_types = _get_query_param(request, "IncludeItemTypes", "").lower()
    recursive = _get_query_param(request, "Recursive", "false").lower() == "true"

    # Edge Case: App Boot Request
    if not any([parent_id, ids_param, search_term, recursive, person_ids, tags_param, filters_string]):
        if "movie" not in item_types and "episode" not in item_types:
            views = await _get_libraries()
            return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})

    if decoded_parent_id and decoded_parent_id.startswith("scene-"):
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})

    # 1. Exact ID Requests (e.g. Resume Watching row)
    if ids_param:
        raw_ids, jellyfin_items = [], []
        server_id = getattr(config, "SERVER_ID", "stash-proxy")
        
        for i in ids_param.split(","):
            dec_i = decode_id(i)
            # Fladder Navigation Folders Intercept
            if dec_i.startswith("root-") or dec_i.startswith("tag-") or dec_i.startswith("filter-"):
                is_root = dec_i.startswith("root-")
                is_nav_folder = dec_i in ["root-filters", "root-tags", "root-alltags"]
                
                if is_root: safe_id = encode_id("root", dec_i.replace("root-", ""))
                elif dec_i.startswith("tag-"): safe_id = encode_id("tag", dec_i.replace("tag-", ""))
                elif dec_i.startswith("filter-"): safe_id = encode_id("filter", dec_i.replace("filter-", ""))
                else: safe_id = i
                
                is_collection = is_root and not is_nav_folder
                folder_item = {"Name": "Folder", "SortName": "Folder", "Id": safe_id, "ServerId": server_id, "Type": "CollectionFolder" if is_collection else "Folder", "IsFolder": True}
                if is_collection: folder_item["CollectionType"] = "movies"
                jellyfin_items.append(folder_item)
                continue

            match = re.search(r'\d+', dec_i)
            if match: raw_ids.append(match.group())
            
        if raw_ids:
            tasks = [stash_client.get_scene(rid) for rid in raw_ids]
            results = await asyncio.gather(*tasks)
            safe_root = encode_id("root", "scenes")
            for scene in results:
                if scene: jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root))
                    
        return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": len(jellyfin_items), "StartIndex": 0})

    # 2. Build GraphQL Filters
    filter_args = {"sort": "created_at", "direction": "DESC"}
    scene_filter = {} 
    original_limit = limit

    if person_ids:
        raw_p_ids = [re.search(r'\d+', decode_id(p)).group() for p in person_ids.split(",") if re.search(r'\d+', decode_id(p))]
        if raw_p_ids: scene_filter["performers"] = {"value": raw_p_ids, "modifier": "INCLUDES"}

    is_folder_override = False
    
    # 3. Dynamic Folder Routing
    if decoded_parent_id:
        if decoded_parent_id in ["root-filters", "root-tags", "root-stashtags", "root-alltags"]:
            jellyfin_items = []
            server_id = getattr(config, "SERVER_ID", "stash-proxy")
            
            if decoded_parent_id == "root-filters":
                filters = await stash_client.get_saved_filters()
                jellyfin_items = [{"Name": f.get("name"), "Id": encode_id("filter", str(f.get("id"))), "Type": "Folder", "IsFolder": True, "ServerId": server_id} for f in filters]
            
            elif decoded_parent_id in ["root-tags", "root-stashtags"]:
                tag_names = getattr(config, "TAG_GROUPS", [])
                if tag_names:
                    all_tags = await stash_client.get_all_tags()
                    for name in tag_names:
                        search_name = name.strip().lower()
                        match = next((t for t in all_tags if t['name'].strip().lower() == search_name), None)
                        if match: jellyfin_items.append({"Name": match['name'], "Id": encode_id("tag", str(match['id'])), "Type": "Folder", "IsFolder": True, "ServerId": server_id})
                if getattr(config, "ENABLE_ALL_TAGS", False): jellyfin_items.append({"Name": "All Tags", "Id": encode_id("root", "alltags"), "Type": "Folder", "IsFolder": True, "ServerId": server_id})
                    
            elif decoded_parent_id == "root-alltags":
                tags = await stash_client.get_all_tags()
                jellyfin_items = [{"Name": t.get("name"), "Id": encode_id("tag", str(t.get("id"))), "Type": "Folder", "IsFolder": True, "ServerId": server_id} for t in tags]
                    
            total_record_count = len(jellyfin_items)
            if original_limit > 0: jellyfin_items = jellyfin_items[start_index : start_index + original_limit]
            return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_record_count, "StartIndex": start_index})

        # Scene Routing Logic
        if decoded_parent_id == "root-scenes": is_folder_override = True
        elif decoded_parent_id == "root-organized":
            scene_filter["organized"] = True
            is_folder_override = True
        elif decoded_parent_id == "root-tagged":
            scene_filter["tags"] = {"modifier": "NOT_NULL"}
            is_folder_override = True
        elif decoded_parent_id == "root-recent":
            cutoff_date = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=getattr(config, "RECENT_DAYS", 14))).strftime("%Y-%m-%dT%H:%M:%S")
            scene_filter["created_at"] = {"value": cutoff_date, "modifier": "GREATER_THAN"}
            is_folder_override = True
        elif decoded_parent_id.startswith("tag-"):
            scene_filter["tags"] = {"value": [decoded_parent_id.replace("tag-", "")], "modifier": "INCLUDES"}
            is_folder_override = True
        elif decoded_parent_id.startswith("person-"):
            scene_filter["performers"] = {"value": [decoded_parent_id.replace("person-", "")], "modifier": "INCLUDES"}
            is_folder_override = True
        elif decoded_parent_id.startswith("studio-"):
            scene_filter["studios"] = {"value": [decoded_parent_id.replace("studio-", "")], "modifier": "INCLUDES"}
            is_folder_override = True
        elif decoded_parent_id.startswith("filter-"):
            is_folder_override = True
            raw_filter_id = decoded_parent_id.replace("filter-", "")
            filters = await stash_client.get_saved_filters()
            data = next((f for f in filters if str(f.get("id")) == raw_filter_id), None)
            
            if data:
                if data.get("object_filter"): scene_filter.update(_transform_saved_filter(data["object_filter"]))
                elif data.get("filter"):
                    import json
                    parsed = json.loads(data["filter"])
                    if "scene_filter" in parsed: scene_filter.update(_transform_saved_filter(parsed["scene_filter"]))
                    if "q" in parsed: filter_args["q"] = parsed["q"]
                    if "sort" in parsed: filter_args["sort"] = parsed["sort"]
                    if "direction" in parsed: filter_args["direction"] = parsed["direction"]
                    
                if data.get("find_filter"):
                    if "q" in data["find_filter"]: filter_args["q"] = data["find_filter"]["q"]
                    if "sort" in data["find_filter"]: filter_args["sort"] = data["find_filter"]["sort"]
                    if "direction" in data["find_filter"]: filter_args["direction"] = data["find_filter"]["direction"]

    if item_types:
        if not any(t in item_types for t in ["movie", "video", "series", "episode", "folder"]):
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})

    # Sort logic
    sort_by = _get_query_param(request, "SortBy", "").lower()
    if "random" in sort_by: filter_args["sort"] = "random"
    elif "datecreated" in sort_by: filter_args["sort"] = "created_at"
    elif "dateplayed" in sort_by: filter_args["sort"] = "updated_at" 
    elif "name" in sort_by or "sortname" in sort_by: filter_args["sort"] = "title"
    if _get_query_param(request, "SortOrder", "").lower() == "ascending": filter_args["direction"] = "ASC"

    # UserData Filter logic
    filter_list = [f.strip() for f in filters_string.split(",")] if filters_string else []
    is_fav = _get_query_param(request, "isFavorite", "").lower()
    is_play = _get_query_param(request, "isPlayed", "").lower()
    
    if "IsFavorite" in filter_list or is_fav == "true": scene_filter["o_counter"] = {"value": 0, "modifier": "GREATER_THAN"}
    elif is_fav == "false": scene_filter["o_counter"] = {"value": 0, "modifier": "EQUALS"}
        
    if "IsUnplayed" in filter_list or is_play == "false": scene_filter["play_count"] = {"value": 0, "modifier": "EQUALS"}
    elif "IsPlayed" in filter_list or is_play == "true": scene_filter["play_count"] = {"value": 0, "modifier": "GREATER_THAN"}

    if "IsResumable" in filter_list:
        filter_args["sort"], filter_args["direction"], limit = "updated_at", "DESC", 100 

    years = _get_query_param(request, "Years")
    if years:
        y_l = [int(re.search(r'\d{4}', decode_id(y)).group()) for y in years.split(",") if re.search(r'\d{4}', decode_id(y))]
        if y_l: scene_filter["date"] = {"value": f"{min(y_l)}-01-01", "value2": f"{max(y_l)}-12-31", "modifier": "BETWEEN"}
        
    if tags_param:
        raw_t = [re.search(r'\d+', decode_id(t)).group() for t in tags_param.split(",") if re.search(r'\d+', decode_id(t))]
        if raw_t: scene_filter["tags"] = {"value": raw_t, "modifier": "INCLUDES"}

    if studio_ids_param:
        raw_s_ids = [re.search(r'\d+', decode_id(s)).group() for s in studio_ids_param.split(",") if re.search(r'\d+', decode_id(s))]
        if raw_s_ids: scene_filter["studios"] = {"value": raw_s_ids, "modifier": "INCLUDES"}

    if search_term: filter_args["q"] = search_term

    if original_limit == 0 and not search_term and "IsResumable" not in filter_list:
        stash_data = await stash_client.fetch_scenes(filter_args, page=1, per_page=1, scene_filter=scene_filter, ignore_sync_level=is_folder_override)
        return JSONResponse({"Items": [], "TotalRecordCount": stash_data.get("count", 0) if stash_data else 0, "StartIndex": start_index})

    # Fetch and format final dataset
    page = (start_index // limit) + 1 if limit > 0 else 1
    stash_data = await stash_client.fetch_scenes(filter_args, page=page, per_page=limit, scene_filter=scene_filter, ignore_sync_level=is_folder_override)
    if not stash_data: return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})
    
    jellyfin_items = []
    safe_root = encode_id("root", "scenes")
    
    for scene in stash_data.get("scenes", []):
        if search_term and not (scene.get("title") or scene.get("code") or "").lower().startswith(search_term.lower()): continue
        if "IsResumable" in filter_list and (not scene.get("resume_time") or scene.get("resume_time") <= 0): continue
        try: jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root))
        except Exception: pass

    total_count = len(jellyfin_items) if search_term or "IsResumable" in filter_list else stash_data.get("count", 0)
    if original_limit > 0 and (search_term or "IsResumable" in filter_list): jellyfin_items = jellyfin_items[start_index : start_index + original_limit]

    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_count, "StartIndex": start_index})

async def endpoint_empty_list(request: Request): return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
async def endpoint_empty_array(request: Request): return JSONResponse([])
async def endpoint_filters(request: Request): return JSONResponse({"Tags": [], "Genres": [], "Studios": [], "OfficialRatings": [], "Years": []})
async def endpoint_theme_songs(request: Request): return JSONResponse({"OwnerId": request.path_params.get("item_id", "unknown"), "Items": [], "TotalRecordCount": 0, "StartIndex": 0})
async def endpoint_special_features(request: Request): return JSONResponse([])

async def endpoint_latest(request: Request):
    """Fetches the 16 most recently added scenes for the home screen rows."""
    parent_id = _get_query_param(request, "ParentId")
    decoded_parent_id = decode_id(parent_id) if parent_id else None
    
    scene_filter, is_folder_override = {}, False
    
    if decoded_parent_id:
        if decoded_parent_id == "root-scenes": is_folder_override = True
        elif decoded_parent_id == "root-organized": scene_filter["organized"], is_folder_override = True, True
        elif decoded_parent_id == "root-tagged": scene_filter["tags"], is_folder_override = {"modifier": "NOT_NULL"}, True
        elif decoded_parent_id == "root-recent":
            cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=getattr(config, "RECENT_DAYS", 14))).strftime("%Y-%m-%dT%H:%M:%S")
            scene_filter["created_at"], is_folder_override = {"value": cutoff, "modifier": "GREATER_THAN"}, True
        elif decoded_parent_id.startswith("tag-"): scene_filter["tags"], is_folder_override = {"value": [decoded_parent_id.replace("tag-", "")], "modifier": "INCLUDES"}, True
            
    stash_data = await stash_client.fetch_scenes({"sort": "created_at", "direction": "DESC"}, page=1, per_page=16, scene_filter=scene_filter, ignore_sync_level=is_folder_override)
    safe_root = encode_id("root", "scenes")
    
    return JSONResponse([jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root) for scene in stash_data.get("scenes", [])])