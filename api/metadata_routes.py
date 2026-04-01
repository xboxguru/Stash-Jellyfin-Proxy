import logging
import datetime
import re
from starlette.responses import JSONResponse, Response
from starlette.requests import Request
import config
from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import encode_id, decode_id, build_folder, generate_image_tag

logger = logging.getLogger(__name__)

async def _build_nav_folder_metadata(decoded_id: str, item_id: str, server_id: str, cache_version: int) -> dict:
    clean_dec = decoded_id.replace("\x00", "").strip()
    is_root = clean_dec.startswith("root-")
    is_nav_folder = clean_dec in ["root-filters", "root-tags", "root-stashtags", "root-alltags"]
    item_name = "Folder"
    
    if is_root: 
        root_map = {
            "root-scenes": "Scenes (Everything)",
            "root-organized": "Scenes (Organized)",
            "root-tagged": "Scenes (Tagged)",
            "root-recent": f"Recently Added ({getattr(config, 'RECENT_DAYS', 14)} Days)",
            "root-filters": "Saved Filters",
            "root-stashtags": "Stash Tags",
            "root-alltags": "All Tags"
        }
        item_name = root_map.get(clean_dec, "Folder")
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
    
    is_collection = is_root and not is_nav_folder
    return build_folder(item_name, item_id, server_id, cache_version, is_collection)

async def endpoint_item_details(request: Request):
    item_id = request.path_params.get("item_id", "")
    decoded_id = decode_id(item_id)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    cache_version = getattr(config, "CACHE_VERSION", 0)
    
    logger.debug(f"Metadata Request -> Item Details for Decoded ID: {decoded_id}")
    
    if "root-" in decoded_id or "tag-" in decoded_id or "filter-" in decoded_id:
        return JSONResponse(await _build_nav_folder_metadata(decoded_id, item_id, server_id, cache_version))

    if decoded_id.startswith("studio-"):
        safe_id = encode_id("studio", decoded_id.replace("studio-", ""))
        return JSONResponse({"Name": "Studio", "SortName": "Studio", "Id": safe_id, "ServerId": server_id, "Type": "Studio", "IsFolder": False})

    if decoded_id.startswith("person-"):
        raw_id = decoded_id.replace("person-", "")
        perf = await stash_client.get_performer(raw_id)
        if perf:
            p_tag = generate_image_tag("person", raw_id, cache_version)
            perf_name = perf.get("name", "Unknown Person")
            return JSONResponse({
                "Name": perf_name, "SortName": perf_name, "Id": item_id, "ServerId": server_id, "Type": "Person", "IsFolder": False,
                "ImageTags": {"Primary": p_tag} if perf.get("image_path") else {}, "HasPrimaryImage": bool(perf.get("image_path")),
                "MovieCount": 1, "ChildCount": 1,
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"Person-{perf_name}", "ItemId": item_id}
            })
        return JSONResponse({"Name": "Person", "SortName": "Person", "Id": item_id, "ServerId": server_id, "Type": "Person", "IsFolder": False})
        
    number_match = re.search(r'\d+', decoded_id)
    if not number_match: 
        logger.warning(f"Invalid ID format requested: {decoded_id}")
        return JSONResponse({"error": f"Invalid ID format: {decoded_id}"}, status_code=400)
        
    scene = await stash_client.get_scene(number_match.group())
    if scene: 
        return JSONResponse(jellyfin_mapper.format_jellyfin_item(scene))
        
    logger.debug(f"Item not found in Stash: {decoded_id}")
    return JSONResponse({"error": "Item not found"}, status_code=404)

async def endpoint_tags(request: Request):
    item_type = "Genre" if "genre" in request.url.path.lower() else "Tag"
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    search_term = next((v.lower() for k, v in request.query_params.items() if k.lower() == "searchterm"), "")
    
    logger.debug(f"Metadata Request -> All {item_type}s (Search Term: '{search_term}')")
    
    stash_tags = await stash_client.get_all_tags()
    if search_term: 
        stash_tags = [t for t in stash_tags if search_term in t.get("name", "").lower()]
    
    jelly_tags = [{"Name": t.get("name"), "Id": encode_id("tag", str(t.get('id'))), "Type": item_type, "ServerId": server_id, "IsFolder": False} for t in stash_tags]
    return JSONResponse({"Items": jelly_tags, "TotalRecordCount": len(jelly_tags), "StartIndex": 0})

async def endpoint_years(request: Request):
    logger.debug("Metadata Request -> Years List")
    current_year = datetime.datetime.now().year
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    years = [{"Name": str(y), "Id": encode_id("year", str(y)), "Type": "Year", "ProductionYear": y, "ServerId": server_id, "IsFolder": False} for y in range(current_year, 1989, -1)]
    return JSONResponse({"Items": years, "TotalRecordCount": len(years), "StartIndex": 0})

async def endpoint_studios(request: Request):
    logger.debug("Metadata Request -> Studios List")
    studios = await stash_client.get_all_studios()
    cache_version = getattr(config, "CACHE_VERSION", 0)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    
    jelly_studios = [{
        "Name": s.get("name"), "Id": encode_id("studio", str(s.get('id'))), "Type": "Studio", "ServerId": server_id, "IsFolder": False, 
        "ImageTags": {"Primary": generate_image_tag("studio", str(s.get('id')), cache_version)}, "HasPrimaryImage": bool(s.get("image_path"))
    } for s in studios]
    return JSONResponse({"Items": jelly_studios, "TotalRecordCount": len(jelly_studios), "StartIndex": 0})

async def endpoint_delete_item(request: Request):
    decoded_id = decode_id(request.path_params.get("item_id", ""))
    deletion_mode = getattr(config, "ALLOW_CLIENT_DELETION", "Disabled").lower()
    
    logger.info(f"Client requested deletion for {decoded_id}. Mode: {deletion_mode}")
    
    if deletion_mode == "disabled": 
        return Response(status_code=403)
    
    if decoded_id.startswith("scene-"):
        raw_id = decoded_id.replace("scene-", "")
        success = await stash_client.destroy_scene(raw_id, delete_file=(deletion_mode == "delete"))
        
        if success:
            logger.info(f"Successfully deleted scene {raw_id}")
            return Response(status_code=204)
        else:
            logger.error(f"Failed to delete scene {raw_id}")
            return Response(status_code=500)

    return Response(status_code=403)