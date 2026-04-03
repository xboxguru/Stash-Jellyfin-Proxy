import logging
import asyncio
import re
import json
from dataclasses import dataclass, asdict
from starlette.responses import JSONResponse, Response
from starlette.requests import Request
import config
import state
from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import encode_id, decode_id, build_folder
from core.query_builder import StashQueryBuilder

logger = logging.getLogger(__name__)

def _get_query_param(request: Request, param_name: str, default=None):
    values = [value for key, value in request.query_params.multi_items() if key.lower() == param_name.lower()]
    return ",".join(values) if values else default

@dataclass
class JellyfinItemQuery:
    parent_id: str
    decoded_parent_id: str
    ids_param: str
    start_index: int
    limit: int
    original_limit: int
    search_term: str
    person_ids: str
    tags_param: str
    studio_ids_param: str
    filters_string: str
    recursive: bool
    item_types: str
    media_types: str
    exclude_types: str
    name_less_than: str
    name_starts_with: str
    name_starts_with_or_greater: str

def _parse_item_query(request: Request) -> JellyfinItemQuery:
    parent_id = _get_query_param(request, "ParentId")
    try: start_index = int(_get_query_param(request, "StartIndex", 0))
    except: start_index = 0
    try: limit = int(_get_query_param(request, "Limit", getattr(config, "DEFAULT_PAGE_SIZE", 50)))
    except: limit = getattr(config, "DEFAULT_PAGE_SIZE", 50)

    return JellyfinItemQuery(
        parent_id=parent_id,
        decoded_parent_id=decode_id(parent_id) if parent_id else None,
        ids_param=_get_query_param(request, "Ids"),
        start_index=start_index,
        limit=limit,
        original_limit=limit,
        search_term=_get_query_param(request, "SearchTerm", ""),
        person_ids=_get_query_param(request, "ArtistIds") or _get_query_param(request, "PeopleIds") or _get_query_param(request, "PersonIds"),
        tags_param=_get_query_param(request, "Tags") or _get_query_param(request, "TagIds") or _get_query_param(request, "GenreIds"),
        studio_ids_param=_get_query_param(request, "StudioIds"),
        filters_string=_get_query_param(request, "Filters", ""),
        recursive="true" in _get_query_param(request, "Recursive", "false").lower(),
        item_types=_get_query_param(request, "IncludeItemTypes", "").lower(),
        media_types=_get_query_param(request, "MediaTypes", "").lower(),
        exclude_types=_get_query_param(request, "ExcludeItemTypes", "").lower(),
        name_less_than=_get_query_param(request, "NameLessThan", ""),
        name_starts_with=_get_query_param(request, "NameStartsWith", ""),
        name_starts_with_or_greater=_get_query_param(request, "NameStartsWithOrGreater", "")
    )

async def _get_libraries():
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    cache_version = getattr(config, "CACHE_VERSION", 0)
    
    views = [
        build_folder("Scenes (Everything)", encode_id("root", "scenes"), server_id, cache_version, is_user_view=True),
        build_folder("Scenes (Organized)", encode_id("root", "organized"), server_id, cache_version, is_user_view=True),
        build_folder("Scenes (Tagged)", encode_id("root", "tagged"), server_id, cache_version, is_user_view=True)
    ]
    
    recent_days = getattr(config, "RECENT_DAYS", 14)
    if recent_days > 0: 
        views.insert(1, build_folder(f"Recently Added ({recent_days} Days)", encode_id("root", "recent"), server_id, cache_version, is_user_view=True))
        
    if getattr(config, "ENABLE_FILTERS", True): 
        views.append(build_folder("Saved Filters", encode_id("root", "filters"), server_id, cache_version, is_user_view=True))
        
    if getattr(config, "ENABLE_TAG_FILTERS", False): 
        views.append(build_folder("Stash Tags", encode_id("root", "stashtags"), server_id, cache_version, is_user_view=True))
    
    tag_names = getattr(config, "TAG_GROUPS", [])
    if tag_names:
        all_tags = await stash_client.get_all_tags()
        for name in tag_names:
            search_name = name.strip().lower()
            match = next((t for t in all_tags if t['name'].strip().lower() == search_name), None)
            if match: 
                views.append(build_folder(match['name'], encode_id("tag", str(match['id'])), server_id, cache_version, is_user_view=True))

    return views

async def endpoint_views(request: Request):
    logger.debug("Router -> Client requested UserViews (Home Screen Libraries)")
    views = await _get_libraries()
    return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})

async def endpoint_virtual_folders(request: Request):
    logger.debug("Router -> Client requested VirtualFolders (Tunarr/ErsatzTV Library Sync)")
    views = await _get_libraries() 
    virtual_folders = [
        {
            "Name": v.get("Name"), 
            "Locations": [], 
            "CollectionType": v.get("CollectionType", "movies"), 
            "LibraryOptions": {"PathInfos": []}, 
            "ItemId": v.get("Id"), 
            "PrimaryImageItemId": v.get("Id"), 
            "RefreshProgress": 0, 
            "RefreshStatus": "Idle"
        } 
        for v in views
    ]
    return JSONResponse(virtual_folders)

async def _handle_global_search(search_term, item_types, media_types, exclude_types, start_index, original_limit):
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    jellyfin_items = []
    
    if "movie" in item_types and "movie" not in exclude_types:
        page = (start_index // original_limit) + 1 if original_limit > 0 else 1
        stash_data = await stash_client.fetch_scenes({"q": search_term, "sort": "created_at", "direction": "DESC"}, page=page, per_page=original_limit, scene_filter={})
        safe_root = jellyfin_mapper.encode_id("root", "scenes") 
        for scene in stash_data.get("scenes", []):
            try: jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=safe_root))
            except Exception: pass

    if "series" in item_types and "series" not in exclude_types:
        query = """query FindPerformers($q: String) { findPerformers(filter: {q: $q, per_page: 20}) { performers { id name } } }"""
        result = await stash_client.call_graphql(query, {"q": search_term})
        for p in result.get("findPerformers", {}).get("performers", []):
            jellyfin_items.append({
                "Name": p.get("name", "Unknown"), "Id": jellyfin_mapper.encode_id("person", str(p.get("id"))),
                "Type": "Series", "IsFolder": True, "ServerId": server_id, "PrimaryImageAspectRatio": 0.6666666666666666, "ImageTags": {"Primary": "primary"} 
            })

    if "video" in media_types and "movie" in exclude_types:
        all_tags = await stash_client.get_all_tags()
        matched_tags = 0
        for t in all_tags:
            if search_term.lower() in t.get("name", "").lower():
                jellyfin_items.append({"Name": t.get("name", ""), "Id": jellyfin_mapper.encode_id("tag", str(t.get("id"))), "Type": "Video", "IsFolder": True, "ServerId": server_id})
                matched_tags += 1
                if matched_tags >= 50: break
                
    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": len(jellyfin_items), "StartIndex": start_index})

async def _handle_exact_ids(ids_param, parent_id):
    raw_ids, jellyfin_items = [], []
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    cache_version = getattr(config, "CACHE_VERSION", 0)
    views = await _get_libraries()
    
    for i in ids_param.split(","):
        clean_i = i.replace("-", "")
        matching_view = next((v for v in views if v["Id"] == clean_i), None)
        if matching_view:
            jellyfin_items.append(matching_view)
            continue

        dec_i = decode_id(i)
        
        if dec_i.startswith("root-") or dec_i.startswith("tag-") or dec_i.startswith("filter-"):
            is_root = dec_i.startswith("root-")
            is_nav_folder = dec_i in ["root-filters", "root-tags", "root-alltags", "root-stashtags"]
            item_name = "Folder"
            
            if is_root: 
                safe_id = i
                if dec_i == "root-alltags": item_name = "All Tags"
            elif dec_i.startswith("tag-"): 
                safe_id = i
                all_tags = await stash_client.get_all_tags()
                match = next((t for t in all_tags if str(t.get("id")) == dec_i.replace("tag-", "")), None)
                if match: item_name = match.get("name", "Folder")
            elif dec_i.startswith("filter-"): 
                safe_id = i
                filters = await stash_client.get_saved_filters()
                match = next((f for f in filters if str(f.get("id")) == dec_i.replace("filter-", "")), None)
                if match: item_name = match.get("name", "Folder")
            else: safe_id = i
            
            is_collection = is_root and not is_nav_folder
            jellyfin_items.append(build_folder(item_name, safe_id, server_id, cache_version, is_collection))
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

async def _handle_virtual_folder_contents(decoded_parent_id, start_index, original_limit):
    if not decoded_parent_id or decoded_parent_id not in ["root-filters", "root-tags", "root-stashtags", "root-alltags"]:
        return None
        
    jellyfin_items = []
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    cache_version = getattr(config, "CACHE_VERSION", 0)

    if decoded_parent_id == "root-filters":
        filters = await stash_client.get_saved_filters()
        jellyfin_items = [build_folder(f.get("name"), encode_id("filter", str(f.get("id"))), server_id, cache_version) for f in filters]
    
    elif decoded_parent_id in ["root-tags", "root-stashtags"]:
        tag_names = getattr(config, "TAG_GROUPS", [])
        if tag_names:
            all_tags = await stash_client.get_all_tags()
            for name in tag_names:
                search_name = name.strip().lower()
                match = next((t for t in all_tags if t['name'].strip().lower() == search_name), None)
                if match: jellyfin_items.append(build_folder(match['name'], encode_id("tag", str(match['id'])), server_id, cache_version))
        if getattr(config, "ENABLE_ALL_TAGS", False): 
            jellyfin_items.append(build_folder("All Tags", encode_id("root", "alltags"), server_id, cache_version))
            
    elif decoded_parent_id == "root-alltags":
        tags = await stash_client.get_all_tags()
        jellyfin_items = [build_folder(t.get("name"), encode_id("tag", str(t.get("id"))), server_id, cache_version) for t in tags]
            
    total_record_count = len(jellyfin_items)
    if original_limit > 0: 
        jellyfin_items = jellyfin_items[start_index : start_index + original_limit]
        
    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_record_count, "StartIndex": start_index})

async def _handle_library_browse(request: Request, query: JellyfinItemQuery):
    virtual_folder_response = await _handle_virtual_folder_contents(query.decoded_parent_id, query.start_index, query.original_limit)
    if virtual_folder_response: 
        return virtual_folder_response
    
    if query.item_types and not any(t in query.item_types for t in ["movie", "folder"]): 
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": query.start_index})
        
    exclude_types = _get_query_param(request, "ExcludeItemTypes", "").lower()
    if exclude_types and "movie" in exclude_types: 
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": query.start_index})

    builder = StashQueryBuilder(request, asdict(query))
    filter_args, scene_filter, _, updated_limit = await builder.build()

    filter_list = [f.strip() for f in query.filters_string.split(",")] if query.filters_string else []

    if query.name_less_than or query.name_starts_with or query.name_starts_with_or_greater:
        filter_args["per_page"] = -1 # We need all records to filter locally
        
        # Fast Path: Client just wants the index count for the alphabet scrollbar (Limit = 0)
        if query.original_limit == 0:
            graphql_query = """query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) { findScenes(filter: $filter, scene_filter: $scene_filter) { scenes { title code files { basename } } } }"""
            data = await stash_client.call_graphql(graphql_query, {"filter": filter_args, "scene_filter": scene_filter})
            scenes = data.get("findScenes", {}).get("scenes", []) if data else []
            
            # --- FIX: Pure mathematical counting (No directional flipping!) ---
            count = 0
            for s in scenes:
                raw_title = s.get("title")
                if not raw_title: 
                    raw_title = s.get("code")
                if not raw_title and s.get("files") and len(s.get("files")) > 0:
                    raw_title = s.get("files")[0].get("basename")
                    
                title = str(raw_title or "").lower().strip()
                
                # --- FIX: Define Wholphin's Sorting Tiers ---
                # If it's empty or starts with a symbol, Wholphin puts it at the absolute bottom
                is_bottom_symbol = not title or not title[0].isalnum()
                
                if query.name_less_than:
                    # Symbols are at the bottom, so they are NEVER less than a letter
                    if not is_bottom_symbol and title < query.name_less_than.lower(): 
                        count += 1
                        
                elif query.name_starts_with_or_greater:
                    # Symbols are at the bottom, so they are ALWAYS greater than any letter
                    if is_bottom_symbol or title >= query.name_starts_with_or_greater.lower(): 
                        count += 1
                        
                elif query.name_starts_with:
                    if not is_bottom_symbol and title.startswith(query.name_starts_with.lower()): 
                        count += 1
                # ---------------------------------------------
            if query.name_less_than:
                logger.info(f"ALPHABET JUMP: Client requested '< {query.name_less_than}' -> Proxy calculated index {count}")    
            return JSONResponse({"Items": [], "TotalRecordCount": count, "StartIndex": 0})
        
        # Slow Path: Client actually clicked the letter to render a filtered view of the items
        else:
            stash_data = await stash_client.fetch_scenes(filter_args, page=1, per_page=-1, scene_filter=scene_filter)
            all_scenes = stash_data.get("scenes", []) if stash_data else []
            
            filtered_scenes = []
            for s in all_scenes:
                title = str(s.get("title") or s.get("code") or "").lower()
                if query.name_less_than and title < query.name_less_than.lower(): filtered_scenes.append(s)
                elif query.name_starts_with and title.startswith(query.name_starts_with.lower()): filtered_scenes.append(s)
                elif query.name_starts_with_or_greater and title >= query.name_starts_with_or_greater.lower(): filtered_scenes.append(s)
            
            total_count = len(filtered_scenes)
            if query.original_limit > 0:
                filtered_scenes = filtered_scenes[query.start_index : query.start_index + query.original_limit]
                
            jellyfin_items = []
            safe_root = encode_id("root", "scenes")
            for scene in filtered_scenes:
                if "IsResumable" in filter_list and (not scene.get("resume_time") or scene.get("resume_time") <= 0): continue
                try: jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=query.parent_id or safe_root))
                except Exception: pass
                
            return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_count, "StartIndex": query.start_index})

    if query.original_limit == 0 and not query.search_term and "IsResumable" not in filter_list:
        stash_data = await stash_client.fetch_scenes(filter_args, page=1, per_page=1, scene_filter=scene_filter)
        return JSONResponse({"Items": [], "TotalRecordCount": stash_data.get("count", 0) if stash_data else 0, "StartIndex": query.start_index})

    page = (query.start_index // updated_limit) + 1 if updated_limit > 0 else 1
    stash_data = await stash_client.fetch_scenes(filter_args, page=page, per_page=updated_limit, scene_filter=scene_filter)
    
    if not stash_data: 
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": query.start_index})
    
    jellyfin_items = []
    safe_root = encode_id("root", "scenes")
    
    for scene in stash_data.get("scenes", []):
        if "IsResumable" in filter_list and (not scene.get("resume_time") or scene.get("resume_time") <= 0): 
            continue
        try: 
            jellyfin_items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=query.parent_id or safe_root))
        except Exception as e: 
            logger.error(f"Failed to map scene {scene.get('id')}: {e}")

    total_count = len(jellyfin_items) if query.search_term or "IsResumable" in filter_list else stash_data.get("count", 0)
    if query.original_limit > 0 and (query.search_term or "IsResumable" in filter_list): 
        jellyfin_items = jellyfin_items[query.start_index : query.start_index + query.original_limit]

    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_count, "StartIndex": query.start_index})

async def endpoint_items(request: Request):
    query = _parse_item_query(request)
    
    if query.search_term and (query.item_types or query.media_types): 
        logger.debug(f"Router -> Global Search Hit. Term: '{query.search_term}'")
        return await _handle_global_search(query.search_term, query.item_types, query.media_types, query.exclude_types, query.start_index, query.original_limit)
        
    if not any([query.parent_id, query.ids_param, query.search_term, query.recursive, query.person_ids, query.tags_param, query.filters_string]):
        if "movie" not in query.item_types and "episode" not in query.item_types:
            logger.debug("Router -> Boot/Root Library Fetch.")
            views = await _get_libraries()
            return JSONResponse({"Items": views, "TotalRecordCount": len(views), "StartIndex": 0})
            
    if query.decoded_parent_id and query.decoded_parent_id.startswith("scene-"): 
        logger.debug("Router -> Intercepted child request on Scene item. Returning empty array.")
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
        
    if query.ids_param: 
        logger.debug(f"Router -> Exact ID Lookup. IDs: {query.ids_param}")
        return await _handle_exact_ids(query.ids_param, query.parent_id)
        
    logger.debug(f"Router -> Standard Library Browse. Parent: {query.decoded_parent_id}")
    return await _handle_library_browse(request, query)

async def endpoint_empty_list(request: Request): return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
async def endpoint_empty_array(request: Request): return JSONResponse([])
async def endpoint_filters(request: Request): return JSONResponse({"Tags": [], "Genres": [], "Studios": [], "OfficialRatings": [], "Years": []})
async def endpoint_theme_songs(request: Request): return JSONResponse({"OwnerId": request.path_params.get("item_id", "unknown"), "Items": [], "TotalRecordCount": 0, "StartIndex": 0})
async def endpoint_special_features(request: Request): return JSONResponse([])

async def endpoint_latest(request: Request):
    parent_id = _get_query_param(request, "ParentId")
    decoded_parent_id = decode_id(parent_id) if parent_id else None
    
    builder = StashQueryBuilder(request, {"decoded_parent_id": decoded_parent_id})
    filter_args, scene_filter, _, _ = await builder.build()
    
    stash_data = await stash_client.fetch_scenes({"sort": "created_at", "direction": "DESC"}, page=1, per_page=16, scene_filter=scene_filter)
    safe_root = encode_id("root", "scenes")
    
    return JSONResponse([jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root) for scene in stash_data.get("scenes", [])])

async def endpoint_search_hints(request: Request):
    response = await endpoint_items(request)
    data = json.loads(response.body.decode('utf-8'))
    hints = [{"ItemId": item["Id"], "Name": item["Name"], "Type": item["Type"], "PrimaryImageTag": item.get("ImageTags", {}).get("Primary", "")} for item in data.get("Items", [])]
    
    search_term = _get_query_param(request, "SearchTerm", "").lower()
    if search_term:
        all_tags = await stash_client.get_all_tags()
        for t in all_tags:
            if search_term in t.get("name", "").lower(): 
                hints.append({"ItemId": encode_id("tag", str(t.get("id"))), "Name": t.get("name", ""), "Type": "Genre"})
                
    return JSONResponse({"SearchHints": hints, "TotalRecordCount": len(hints)})

async def endpoint_display_preferences(request: Request):
    display_id = request.path_params.get("display_id", "default").replace("-", "")
    if request.method == "POST":
        try:
            data = await request.json()
            state.display_preferences[display_id] = data
            state.save_prefs()
        except Exception as e: 
            logger.error(f"Failed to save display preferences: {e}")
        return Response(status_code=204)
        
    saved_prefs = state.display_preferences.get(display_id)
    if saved_prefs:
        saved_prefs["Id"] = display_id
        return JSONResponse(saved_prefs)
        
    return JSONResponse({"Id": display_id, "Client": "emby", "SortBy": "Default", "SortOrder": "Ascending", "RememberIndexing": False, "CustomPrefs": {}})