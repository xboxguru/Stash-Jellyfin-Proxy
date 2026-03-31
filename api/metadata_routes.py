import logging
import datetime
import re
import hashlib
from starlette.responses import JSONResponse, Response
from starlette.requests import Request
import config
from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import encode_id, decode_id

logger = logging.getLogger(__name__)

async def endpoint_item_details(request: Request):
    """Provides detailed metadata for a single item (Scene, Folder, Person, Studio)."""
    item_id = request.path_params.get("item_id", "")
    decoded_id = decode_id(item_id)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    cache_version = getattr(config, "CACHE_VERSION", 0)
    
    # 1. Handle Navigation Folders
    if "root-" in decoded_id or "tag-" in decoded_id or "filter-" in decoded_id:
        
        # THE FIX: Violently scrub invisible null bytes and whitespace before matching
        clean_dec = decoded_id.replace("\x00", "").strip()
        
        is_root = clean_dec.startswith("root-")
        is_nav_folder = clean_dec in ["root-filters", "root-tags", "root-stashtags", "root-alltags"]
        
        item_name = "Folder"
        safe_id = item_id # Echo exact requested ID
        
        # Dynamically resolve real names using our sanitized string
        if is_root: 
            if clean_dec == "root-scenes": item_name = "Scenes (Everything)"
            elif clean_dec == "root-organized": item_name = "Scenes (Organized)"
            elif clean_dec == "root-tagged": item_name = "Scenes (Tagged)"
            elif clean_dec == "root-recent": item_name = f"Recently Added ({getattr(config, 'RECENT_DAYS', 14)} Days)"
            elif clean_dec == "root-filters": item_name = "Saved Filters"
            elif clean_dec == "root-stashtags": item_name = "Stash Tags"
            elif clean_dec == "root-alltags": item_name = "All Tags"
        elif clean_dec.startswith("tag-"): 
            raw_id = clean_dec.replace("tag-", "")
            all_tags = await stash_client.get_all_tags()
            match = next((t for t in all_tags if str(t.get("id")) == raw_id), None)
            if match: item_name = match.get("name", "Folder")
        elif clean_dec.startswith("filter-"): 
            raw_id = clean_dec.replace("filter-", "")
            filters = await stash_client.get_saved_filters()
            match = next((f for f in filters if str(f.get("id")) == raw_id), None)
            if match: item_name = match.get("name", "Folder")
        
        logo_hash = hashlib.md5(f"stash-logo-{cache_version}".encode()).hexdigest()
        is_collection = is_root and not is_nav_folder
        
        response_dict = {
            "Name": item_name, 
            "SortName": item_name, 
            "Id": safe_id, 
            "DisplayPreferencesId": safe_id,
            "ServerId": server_id, 
            "Type": "CollectionFolder" if is_collection else "Folder", 
            "IsFolder": True,
            "PrimaryImageAspectRatio": 1.7777777777777777,
            "ImageTags": {"Primary": logo_hash, "Thumb": logo_hash},
            "HasPrimaryImage": True, 
            "HasThumb": True,
            "HasBackdrop": True,
            "BackdropImageTags": [logo_hash]
        }
        if is_collection: response_dict["CollectionType"] = "movies"
        return JSONResponse(response_dict)

    # 2. Handle Studios
    if decoded_id.startswith("studio-"):
        safe_id = encode_id("studio", decoded_id.replace("studio-", ""))
        return JSONResponse({"Name": "Studio", "SortName": "Studio", "Id": safe_id, "ServerId": server_id, "Type": "Studio", "IsFolder": False})

    # 3. Handle Performers
    if decoded_id.startswith("person-"):
        raw_id = decoded_id.replace("person-", "")
        perf = await stash_client.get_performer(raw_id)
        
        if perf:
            p_tag = hashlib.md5(f"person-{raw_id}-v{cache_version}".encode()).hexdigest()
            perf_name = perf.get("name", "Unknown Person")
            return JSONResponse({
                "Name": perf_name, "SortName": perf_name, "Id": item_id, "ServerId": server_id, "Type": "Person", "IsFolder": False,
                "ImageTags": {"Primary": p_tag} if perf.get("image_path") else {}, "HasPrimaryImage": bool(perf.get("image_path")),
                "MovieCount": 1, "ChildCount": 1,
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"Person-{perf_name}", "ItemId": item_id}
            })
        return JSONResponse({"Name": "Person", "SortName": "Person", "Id": item_id, "ServerId": server_id, "Type": "Person", "IsFolder": False})
        
    # 4. Handle Scenes
    number_match = re.search(r'\d+', decoded_id)
    if not number_match: return JSONResponse({"error": f"Invalid ID format: {decoded_id}"}, status_code=400)
        
    raw_id = number_match.group()
    scene = await stash_client.get_scene(raw_id)
    
    if scene: return JSONResponse(jellyfin_mapper.format_jellyfin_item(scene))
    return JSONResponse({"error": "Item not found"}, status_code=404)

async def endpoint_tags(request: Request):
    """Provides a list of all Stash tags formatted as Jellyfin Genres/Tags, with search filtering."""
    item_type = "Genre" if "genre" in request.url.path.lower() else "Tag"
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    
    # Extract search term safely (case-insensitive key check)
    search_term = next((v.lower() for k, v in request.query_params.items() if k.lower() == "searchterm"), "")
    
    stash_tags = await stash_client.get_all_tags()
    
    # Filter tags by search term if one was typed in
    if search_term:
        stash_tags = [t for t in stash_tags if search_term in t.get("name", "").lower()]
    
    jelly_tags = [{"Name": t.get("name"), "Id": encode_id("tag", str(t.get('id'))), "Type": item_type, "ServerId": server_id, "IsFolder": False} for t in stash_tags]
    return JSONResponse({"Items": jelly_tags, "TotalRecordCount": len(jelly_tags), "StartIndex": 0})

async def endpoint_years(request: Request):
    current_year = datetime.datetime.now().year
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    years = [{"Name": str(y), "Id": encode_id("year", str(y)), "Type": "Year", "ProductionYear": y, "ServerId": server_id, "IsFolder": False} for y in range(current_year, 1989, -1)]
    return JSONResponse({"Items": years, "TotalRecordCount": len(years), "StartIndex": 0})

async def endpoint_studios(request: Request):
    studios = await stash_client.get_all_studios()
    cache_version = getattr(config, "CACHE_VERSION", 0)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    
    jelly_studios = [{
        "Name": s.get("name"), "Id": encode_id("studio", str(s.get('id'))), "Type": "Studio", "ServerId": server_id, "IsFolder": False, 
        "ImageTags": {"Primary": hashlib.md5(f"studio-{s.get('id')}-v{cache_version}".encode()).hexdigest()}, "HasPrimaryImage": bool(s.get("image_path"))
    } for s in studios]
    return JSONResponse({"Items": jelly_studios, "TotalRecordCount": len(jelly_studios), "StartIndex": 0})

async def endpoint_delete_item(request: Request):
    """Intercepts Jellyfin delete requests and safely executes them in Stash."""
    decoded_id = decode_id(request.path_params.get("item_id", ""))
    deletion_mode = getattr(config, "ALLOW_CLIENT_DELETION", "Disabled").lower()
    
    if deletion_mode == "disabled":
        logger.warning(f"🚫 Deletion attempted for {decoded_id}, but it is disabled in the proxy config.")
        return Response(status_code=403)
    
    if decoded_id.startswith("scene-"):
        raw_id = decoded_id.replace("scene-", "")
        nuke_file = (deletion_mode == "delete")
        
        logger.info(f"🗑️ DELETE REQUEST: {'NUKING' if nuke_file else 'REMOVING'} Scene {raw_id}...")
        success = await stash_client.destroy_scene(raw_id, delete_file=nuke_file)
        
        if success:
            logger.info(f"✅ SUCCESS: Scene {raw_id} successfully deleted!")
            return Response(status_code=204)
        else:
            logger.error(f"❌ STASH DELETION ERROR: Failed to destroy scene {raw_id}")
            return Response(status_code=500)

    logger.warning(f"🚫 Denied deletion request for non-scene item: {decoded_id}")
    return Response(status_code=403)