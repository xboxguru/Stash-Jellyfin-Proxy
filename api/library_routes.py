import logging
import asyncio
import re
import json
import datetime
import random
from dataclasses import dataclass, asdict
from starlette.responses import JSONResponse, Response
from starlette.requests import Request
import config
import state
from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import encode_id, decode_id, build_folder, generate_sort_name
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
        # Native Pagination Fix: Push the start_index math to Stash
        page = (start_index // original_limit) + 1 if original_limit > 0 else 1
        limit = original_limit if original_limit > 0 else -1
        
        tags_data = await stash_client.fetch_tags(page=page, per_page=limit)
        jellyfin_items = [
            build_folder(t.get("name"), encode_id("tag", str(t.get("id"))), server_id, cache_version) 
            for t in tags_data.get("tags", [])
        ]
        
        # We can return immediately, bypassing the old Python slicing logic
        return JSONResponse({
            "Items": jellyfin_items, 
            "TotalRecordCount": tags_data.get("count", 0), 
            "StartIndex": start_index
        })
            
    total_record_count = len(jellyfin_items)
    if original_limit > 0: 
        jellyfin_items = jellyfin_items[start_index : start_index + original_limit]
        
    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_record_count, "StartIndex": start_index})

async def _handle_library_browse(request: Request, query: JellyfinItemQuery):
    virtual_folder_response = await _handle_virtual_folder_contents(query.decoded_parent_id, query.start_index, query.original_limit)
    if virtual_folder_response: 
        return virtual_folder_response
    
    is_performer_parent = bool(query.decoded_parent_id and query.decoded_parent_id.startswith("person-"))

    # Performer-as-Series workaround: when Wholphin treats a performer as a Series and requests Seasons,
    # return a single fake "All Scenes" Season so the user can drill in and see the scenes.
    if is_performer_parent and query.item_types and "season" in query.item_types:
        performer_id = query.decoded_parent_id.replace("person-", "")
        perf = await stash_client.get_performer(performer_id)
        if perf:
            scene_data = await stash_client.fetch_scenes(
                {"sort": "created_at", "direction": "DESC"}, page=1, per_page=1,
                scene_filter={"performers": {"value": [performer_id], "modifier": "INCLUDES"}}
            )
            scene_count = scene_data.get("count", 0) if scene_data else 0
            return JSONResponse({
                "Items": [{
                    "Name": "All Scenes",
                    "Id": query.parent_id,
                    "SeriesId": query.parent_id,
                    "SeriesName": perf.get("name", "Unknown"),
                    "Type": "Season",
                    "IsFolder": True,
                    "IndexNumber": 1,
                    "ChildCount": scene_count,
                    "ServerId": getattr(config, "SERVER_ID", "stash-proxy"),
                }],
                "TotalRecordCount": 1,
                "StartIndex": 0
            })
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": query.start_index})

    serve_as_episodes = is_performer_parent and bool(query.item_types and "episode" in query.item_types)

    if query.item_types and not any(t in query.item_types for t in ["movie", "folder"]):
        if not serve_as_episodes:
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": query.start_index})

    exclude_types = _get_query_param(request, "ExcludeItemTypes", "").lower()
    if exclude_types and "movie" in exclude_types:
        if not serve_as_episodes:
            return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": query.start_index})

    builder = StashQueryBuilder(request, asdict(query))
    filter_args, scene_filter, _, updated_limit = await builder.build()

    filter_list = [f.strip() for f in query.filters_string.split(",")] if query.filters_string else []

    if query.name_less_than or query.name_starts_with or query.name_starts_with_or_greater:
        # Use our RAM Cache instead of fetching heavy items!
        filter_str = json.dumps(filter_args, sort_keys=True)
        scene_filter_str = json.dumps(scene_filter, sort_keys=True)
        all_lightweight_scenes = await stash_client.fetch_lightweight_index(filter_str, scene_filter_str)
        
        matching_ids = []
        for s in all_lightweight_scenes:
            raw_title = s.get("title") or s.get("code")
            if not raw_title and s.get("files") and len(s.get("files")) > 0:
                raw_title = s.get("files")[0].get("basename")
                
            sort_title = generate_sort_name(raw_title)
            
            if query.name_less_than:
                if sort_title < query.name_less_than.lower(): 
                    matching_ids.append(s["id"])
            elif query.name_starts_with_or_greater:
                if sort_title >= query.name_starts_with_or_greater.lower(): 
                    matching_ids.append(s["id"])
            elif query.name_starts_with:
                if sort_title.startswith(query.name_starts_with.lower()): 
                    matching_ids.append(s["id"])

        total_count = len(matching_ids)

        # Fast Path: Client just wants the index count
        if query.original_limit == 0:
            return JSONResponse({"Items": [], "TotalRecordCount": total_count, "StartIndex": 0})
        
        # Slow Path: Client clicked a letter. Fetch full data ONLY for the current page
        page_ids = matching_ids[query.start_index : query.start_index + query.original_limit] if query.original_limit > 0 else matching_ids
        
        filtered_scenes = []
        if page_ids:
            # Fire concurrent requests for the exact IDs
            tasks = [stash_client.get_scene(sid) for sid in page_ids]
            unordered_scenes = await asyncio.gather(*tasks)
            
            # Map and preserve our strict Python sorting order
            scene_map = {str(scene["id"]): scene for scene in unordered_scenes if scene}
            filtered_scenes = [scene_map[str(sid)] for sid in page_ids if str(sid) in scene_map]
            
        jellyfin_items = []
        safe_root = encode_id("root", "scenes")
        for scene in filtered_scenes:
            if "IsResumable" in filter_list and (not scene.get("resume_time") or scene.get("resume_time") <= 0): continue
            try:
                item = jellyfin_mapper.format_jellyfin_item(scene, parent_id=query.parent_id or safe_root)
                if serve_as_episodes:
                    item["Type"] = "Episode"
                    item["SeriesId"] = query.parent_id
                    item["SeasonId"] = query.parent_id
                jellyfin_items.append(item)
            except Exception: pass
            
        return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total_count, "StartIndex": query.start_index})

    if query.original_limit == 0 and not query.search_term and "IsResumable" not in filter_list:
        stash_data = await stash_client.fetch_scenes(filter_args, page=1, per_page=1, scene_filter=scene_filter)
        return JSONResponse({"Items": [], "TotalRecordCount": stash_data.get("count", 0) if stash_data else 0, "StartIndex": query.start_index})

    is_alpha_sort = filter_args.get("sort") in ["title", "name"]
    
    if is_alpha_sort:
        # Use our cache to get the full index so Python can sort it perfectly
        filter_str = json.dumps(filter_args, sort_keys=True)
        scene_filter_str = json.dumps(scene_filter, sort_keys=True)
        all_lightweight_scenes = await stash_client.fetch_lightweight_index(filter_str, scene_filter_str)
        
        # Sort the entire library using our master sanitizer
        def get_sort_key(s):
            raw_title = s.get("title") or s.get("code")
            if not raw_title and s.get("files") and len(s.get("files")) > 0:
                raw_title = s.get("files")[0].get("basename")
            return generate_sort_name(raw_title)
            
        all_lightweight_scenes.sort(key=get_sort_key, reverse=(filter_args.get("direction") == "DESC"))
        
        total_count = len(all_lightweight_scenes)
        page_scenes = all_lightweight_scenes[query.start_index : query.start_index + updated_limit] if updated_limit > 0 else all_lightweight_scenes
        page_ids = [s["id"] for s in page_scenes]
        
        # Fetch full data ONLY for the current page
        stash_data = {"count": total_count, "scenes": []}
        if page_ids:
            # Fire concurrent requests for the exact IDs
            tasks = [stash_client.get_scene(sid) for sid in page_ids]
            unordered_scenes = await asyncio.gather(*tasks)
            
            # Map and preserve our strict Python sorting order
            scene_map = {str(scene["id"]): scene for scene in unordered_scenes if scene}
            stash_data["scenes"] = [scene_map[str(sid)] for sid in page_ids if str(sid) in scene_map]
            
    else:
        # Standard database pagination for Date, Rating, Random, etc.
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
            item = jellyfin_mapper.format_jellyfin_item(scene, parent_id=query.parent_id or safe_root)
            if serve_as_episodes:
                item["Type"] = "Episode"
                item["SeriesId"] = query.parent_id
                item["SeasonId"] = query.parent_id
            jellyfin_items.append(item)
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

async def endpoint_filters(request: Request):
    tags = await stash_client.get_all_tags()
    studios = await stash_client.get_all_studios()
    tag_names = sorted([t.get("name", "") for t in tags if t.get("name")])
    studio_names = sorted([s.get("name", "") for s in studios if s.get("name")])
    current_year = datetime.datetime.now().year
    years = list(range(current_year, 1989, -1))
    return JSONResponse({"Tags": tag_names, "Genres": tag_names, "Studios": studio_names, "OfficialRatings": ["XXX"], "Years": years})
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

async def endpoint_resume(request: Request):
    parent_id = _get_query_param(request, "ParentId")
    decoded_parent_id = decode_id(parent_id) if parent_id else None
    try:
        limit = int(_get_query_param(request, "Limit", "12"))
    except (ValueError, TypeError):
        limit = 12

    builder = StashQueryBuilder(request, {"decoded_parent_id": decoded_parent_id})
    _, scene_filter, _, _ = await builder.build()

    stash_data = await stash_client.fetch_scenes(
        {"sort": "updated_at", "direction": "DESC"},
        page=1, per_page=100,
        scene_filter=scene_filter
    )

    safe_root = encode_id("root", "scenes")
    items = []
    for scene in stash_data.get("scenes", []):
        resume_time = scene.get("resume_time") or 0
        if resume_time <= 0:
            continue
        files = scene.get("files") or []
        if files:
            duration = files[0].get("duration") or 0
            if duration > 0 and (resume_time / duration) >= 0.90:
                continue
        try:
            items.append(jellyfin_mapper.format_jellyfin_item(scene, parent_id=parent_id or safe_root))
        except Exception as e:
            logger.error(f"Failed to map resume scene {scene.get('id')}: {e}")
        if len(items) >= limit:
            break

    return JSONResponse({"Items": items, "TotalRecordCount": len(items), "StartIndex": 0})

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
    user_id = request.path_params.get("user_id", "shared").replace("-", "")
    pref_key = f"{user_id}_{display_id}"

    if request.method == "POST":
        try:
            data = await request.json()
            state.display_preferences[pref_key] = data
            state.save_prefs()
        except Exception as e:
            logger.error(f"Failed to save display preferences: {e}")
        return Response(status_code=204)

    saved_prefs = state.display_preferences.get(pref_key)
    if saved_prefs:
        saved_prefs["Id"] = display_id
        return JSONResponse(saved_prefs)

    return JSONResponse({"Id": display_id, "Client": "emby", "SortBy": "Default", "SortOrder": "Ascending", "RememberIndexing": False, "RememberSorting": False, "ScrollDirection": "Horizontal", "ShowBackdrop": True, "ShowSidebar": False, "PrimaryImageHeight": 213, "PrimaryImageWidth": 160, "CustomPrefs": {}})

# --- SMART DISCOVERY / NEXT UP LOGIC ---

async def _fetch_affinity_scenes(field: str, item_id: str, limit: int, unwatched_only: bool = True, sort_dir: str = "ASC") -> list:
    """Helper: Fetches scenes for a specific performer, studio, or tag."""
    scene_filter = {field: {"value": [item_id], "modifier": "INCLUDES"}}
    
    if unwatched_only:
        scene_filter["play_count"] = {"value": 0, "modifier": "EQUALS"}
        
    data = await stash_client.fetch_scenes(
        filter_args={"sort": "date", "direction": sort_dir},
        page=1, per_page=limit,
        scene_filter=scene_filter
    )
    return data.get("scenes", []) if data else []


async def _build_similar_pool(scene_id: str, target_limit: int = 12) -> list:
    """Orchestrator: Fetches scenes sharing performers, studios, or tags with the target scene."""
    raw_id = scene_id.replace("scene-", "") if scene_id.startswith("scene-") else scene_id
    scene = await stash_client.get_scene(raw_id)
    
    if not scene:
        return []
        
    performer_ids = [p["id"] for p in scene.get("performers", []) if p.get("id")]
    studio_id = scene.get("studio", {}).get("id") if scene.get("studio") else None
    tag_ids = [t["id"] for t in scene.get("tags", []) if t.get("id")]
    
    # Randomly select up to 3 tags to prevent firing 50 queries for heavily-tagged scenes
    sample_tags = random.sample(tag_ids, min(len(tag_ids), 10))
    
    fetch_tasks = []
    
    # 1. Fetch 5 recent scenes from each Performer
    for p_id in performer_ids:
        fetch_tasks.append(_fetch_affinity_scenes("performers", p_id, 5, unwatched_only=False, sort_dir="DESC"))
    
    # 2. Fetch 5 recent scenes from the Studio
    if studio_id:
        fetch_tasks.append(_fetch_affinity_scenes("studios", studio_id, 5, unwatched_only=False, sort_dir="DESC"))
        
    # 3. Fetch 3 recent scenes for the sampled Tags
    for t_id in sample_tags:
        fetch_tasks.append(_fetch_affinity_scenes("tags", t_id, 10, unwatched_only=False, sort_dir="DESC"))
        
    if not fetch_tasks:
        return []
        
    results = await asyncio.gather(*fetch_tasks)
    
    # 4. Deduplicate and ensure we don't return the exact scene the user is currently looking at
    candidates = {}
    for scene_list in results:
        for s in scene_list:
            s_id = s.get("id")
            if s_id and str(s_id) != str(raw_id) and s_id not in candidates:
                candidates[s_id] = s
                
    # 5. Shuffle and return
    pool = list(candidates.values())
    random.shuffle(pool)
    return pool[:target_limit]


async def endpoint_similar_items(request: Request):
    """Route: Maps the similar Stash scenes to Jellyfin items."""
    item_id = request.path_params.get("item_id", "")
    try:
        limit = int(_get_query_param(request, "Limit", "12"))
    except ValueError:
        limit = 12
        
    logger.notice(f"Router -> Similar Items Requested for {item_id} (Limit: {limit})")
    
    decoded_id = decode_id(item_id)
    
    # In our ecosystem, "Similar" only applies to scenes, not folders or individual performers
    if not decoded_id.startswith("scene-"):
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": 0})
        
    scenes = await _build_similar_pool(decoded_id, target_limit=limit)
    
    jellyfin_items = []
    safe_root = encode_id("root", "scenes")
    
    for scene in scenes:
        try:
            item = jellyfin_mapper.format_jellyfin_item(scene, parent_id=safe_root)
            jellyfin_items.append(item)
        except Exception as e:
            logger.error(f"Failed to map Similar scene {scene.get('id')}: {e}")
    
    logger.notice(f"Similar pool generated {len(jellyfin_items)} scenes for {decoded_id}.")

    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": len(jellyfin_items), "StartIndex": 0})

async def endpoint_shows_episodes(request: Request):
    """Route: Handles TvShowsApi.getEpisodes for performer-as-Series. Returns performer scenes as Episode items."""
    series_id = request.path_params.get("series_id", "")
    decoded = decode_id(series_id)

    try:
        start_index = int(_get_query_param(request, "startIndex") or _get_query_param(request, "StartIndex") or 0)
    except (ValueError, TypeError):
        start_index = 0
    try:
        limit = int(_get_query_param(request, "limit") or _get_query_param(request, "Limit") or getattr(config, "DEFAULT_PAGE_SIZE", 50))
    except (ValueError, TypeError):
        limit = getattr(config, "DEFAULT_PAGE_SIZE", 50)

    logger.notice(f"Router -> Shows/Episodes: series_id={series_id} decoded={decoded} start={start_index} limit={limit}")

    if not decoded.startswith("person-"):
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})

    performer_id = decoded.replace("person-", "")
    page = (start_index // limit) + 1 if limit > 0 else 1

    scene_filter = {"performers": {"value": [performer_id], "modifier": "INCLUDES"}}
    sync_mode = getattr(config, "SYNC_LEVEL", "Everything")
    if sync_mode == "Organized":
        scene_filter["organized"] = True
    elif sync_mode == "Tagged":
        scene_filter["tags"] = {"modifier": "NOT_NULL"}

    scene_data = await stash_client.fetch_scenes(
        {"sort": "created_at", "direction": "DESC"}, page=page, per_page=limit, scene_filter=scene_filter
    )

    if not scene_data:
        return JSONResponse({"Items": [], "TotalRecordCount": 0, "StartIndex": start_index})

    total = scene_data.get("count", 0)
    scenes = scene_data.get("scenes") or []

    jellyfin_items = []
    for scene in scenes:
        try:
            item = jellyfin_mapper.format_jellyfin_item(scene, parent_id=series_id)
            item["Type"] = "Episode"
            item["SeriesId"] = series_id
            item["SeasonId"] = series_id
            jellyfin_items.append(item)
        except Exception as e:
            logger.error(f"Failed to map episode scene {scene.get('id')}: {e}")

    logger.notice(f"Shows/Episodes: returning {len(jellyfin_items)}/{total} scenes for performer {performer_id}")
    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": total, "StartIndex": start_index})

async def _build_next_up_pool(target_limit: int = 25) -> list:
    """Harvests affinities from native Stash watch history and builds a randomized pool of unwatched scenes."""
    
    # 1. Fetch recent watch history directly from Stash's native database
    recent_scenes = await stash_client.fetch_recent_watch_history(limit=50)
    logger.notice(f"Building Next Up pool. Target: {target_limit}. Found {len(recent_scenes)} recent watches.")

    candidates = {}
    fetch_tasks = []

    # 2. Extract affinity IDs (Performers and Studios) from recently watched scenes
    for scene in recent_scenes:
        for p in scene.get("performers", []):
            if p.get("id"):
                fetch_tasks.append(_fetch_affinity_scenes("performers", p["id"], 5))
                
        if scene.get("studio") and scene.get("studio").get("id"):
            fetch_tasks.append(_fetch_affinity_scenes("studios", scene["studio"]["id"], 5))

    # 3. Fire all GraphQL requests concurrently
    if fetch_tasks:
        results = await asyncio.gather(*fetch_tasks)
        for scene_list in results:
            for s in scene_list:
                s_id = s.get("id")
                if s_id and s_id not in candidates:
                    candidates[s_id] = s

    # 4. Backfill logic (if affinity pool is short)
    if len(candidates) < target_limit:
        shortfall = target_limit - len(candidates)
        logger.notice(f"Affinity pool short by {shortfall} scenes. Backfilling with global unwatched scenes.")
        try:
            backfill_data = await stash_client.fetch_scenes(
                filter_args={"sort": "date", "direction": "DESC"},
                page=1, per_page=shortfall + 10,
                scene_filter={"play_count": {"value": 0, "modifier": "EQUALS"}}
            )
            for s in backfill_data.get("scenes", []):
                if len(candidates) >= target_limit: break
                if s["id"] not in candidates:
                    candidates[s["id"]] = s
        except Exception as e:
            logger.error(f"Failed to fetch backfill scenes for Next Up: {e}")

    # 5. Shuffle and slice the target limit
    pool = list(candidates.values())
    random.shuffle(pool)
    final_pool = pool[:target_limit]
    
    logger.notice(f"Next Up pool generation complete. Selected {len(final_pool)} scenes from {len(candidates)} candidates.")
    return final_pool

async def endpoint_next_up(request: Request):
    try:
        limit = int(_get_query_param(request, "Limit", "24"))
    except ValueError:
        limit = 24
        
    logger.notice(f"Router -> Next Up Discovery Requested (Limit: {limit})")
    
    scenes = await _build_next_up_pool(target_limit=limit)
    
    jellyfin_items = []
    safe_root = encode_id("root", "scenes")
    
    for scene in scenes:
        try:
            item = jellyfin_mapper.format_jellyfin_item(scene, parent_id=safe_root)
            
            # MAP OVERRIDE: Force strict clients to render this in the Next Up row
            item["Type"] = "Episode"
            
            # Treat the Studio like a TV Network/Series Name for UI polish
            series_name = scene.get("studio", {}).get("name") if scene.get("studio") else "Stash Discovery"
            item["SeriesName"] = series_name
            
            jellyfin_items.append(item)
        except Exception as e:
            logger.error(f"Failed to map Next Up scene {scene.get('id')}: {e}")
            
    return JSONResponse({"Items": jellyfin_items, "TotalRecordCount": len(jellyfin_items), "StartIndex": 0})