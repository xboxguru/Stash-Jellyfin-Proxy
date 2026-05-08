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
        raw_id = decoded_id.replace("studio-", "")
        safe_id = encode_id("studio", raw_id)
        
        logger.debug(f"RICH STUDIO HIT: Jellyfin requested studio ID '{raw_id}'. Querying Stash...")
        
        query = "query($id: ID!) { findStudio(id: $id) { name details image_path url parent_studio { name } } }"
        data = await stash_client.call_graphql(query, {"id": raw_id})
        studio = data.get("findStudio") if data else None
        
        logger.debug(f"RICH STUDIO RESULT: {studio}")

        if studio:
            s_tag = generate_image_tag("studio", raw_id, cache_version)
            studio_name = studio.get("name", "Unknown Studio")
            
            overview_parts = []
            if studio.get("parent_studio"): overview_parts.append(f"Parent Studio: {studio['parent_studio']['name']}")
            if studio.get("url"): overview_parts.append(f"Website: {studio['url']}")
            if studio.get("details"): overview_parts.append(f"\n{studio['details']}")

            return JSONResponse({
                "Name": studio_name, 
                "SortName": studio_name, 
                "Id": safe_id, 
                "ServerId": server_id, 
                "Type": "Studio", 
                "IsFolder": False,
                "Overview": "\n".join(overview_parts),
                "ImageTags": {"Primary": s_tag} if studio.get("image_path") else {},
                "HasPrimaryImage": bool(studio.get("image_path"))
            })
            
        return JSONResponse({"Name": "Studio", "SortName": "Studio", "Id": safe_id, "ServerId": server_id, "Type": "Studio", "IsFolder": False})

    if decoded_id.startswith("person-"):
        raw_id = decoded_id.replace("person-", "")
        
        logger.debug(f"RICH CAST HIT: Jellyfin requested person ID '{raw_id}'. Querying Stash...")
        
        perf = await stash_client.get_performer(raw_id)
        
        logger.debug(f"RICH CAST RESULT: {perf}")
        
        if perf:
            p_tag = generate_image_tag("person", raw_id, cache_version)
            perf_name = perf.get("name", "Unknown Person")
            
            # --- RICH CAST UPDATE: Build IMDb-style Biography ---
            bio_parts = []
            
            if perf.get("alias_list"): bio_parts.append(f"Aliases: {', '.join(perf['alias_list'])}")
            if perf.get("gender"): bio_parts.append(f"Gender: {perf['gender']}")
            if perf.get("career_length"): bio_parts.append(f"Career Length: {perf['career_length']}")
            if perf.get("country"): bio_parts.append(f"Country: {perf['country']}")
            if perf.get("ethnicity"): bio_parts.append(f"Ethnicity: {perf['ethnicity']}")
            if perf.get("hair_color"): bio_parts.append(f"Hair: {perf['hair_color']}")
            if perf.get("eye_color"): bio_parts.append(f"Eyes: {perf['eye_color']}")
            
            # Height Conversion (cm to ft/in)
            height_cm = perf.get("height_cm")
            if height_cm:
                try:
                    total_inches = round(int(height_cm) / 2.54)
                    ft = total_inches // 12
                    inch = total_inches % 12
                    bio_parts.append(f"Height: {height_cm} cm ({ft}' {inch}\")")
                except ValueError:
                    bio_parts.append(f"Height: {height_cm} cm")
            
            if perf.get("weight"): bio_parts.append(f"Weight: {perf['weight']} kg")
            if perf.get("measurements"): bio_parts.append(f"Measurements: {perf['measurements']}")
            if perf.get("fake_tits"): bio_parts.append(f"Fake Tits: {perf['fake_tits']}")
            
            # Penis Length Conversion (cm to in)
            penis_cm = perf.get("penis_length")
            if penis_cm:
                try:
                    p_inches = round(float(penis_cm) / 2.54, 1)
                    bio_parts.append(f"Penis Length: {penis_cm} cm ({p_inches}\")")
                except ValueError:
                    bio_parts.append(f"Penis Length: {penis_cm} cm")
            
            if perf.get("circumcised"): bio_parts.append(f"Circumcised: {str(perf['circumcised']).capitalize()}")
            if perf.get("piercings"): bio_parts.append(f"Piercings: {perf['piercings']}")
            if perf.get("tattoos"): bio_parts.append(f"Tattoos: {perf['tattoos']}")
            
            # Add a double line break before the main details paragraph
            if perf.get("details"): bio_parts.append(f"\n\n{perf['details']}") 
            
            premiere_date = f"{perf['birthdate']}T00:00:00.0000000Z" if perf.get("birthdate") else None
            
            return JSONResponse({
                "Name": perf_name, 
                "SortName": perf_name, 
                "Id": item_id, 
                "ServerId": server_id, 
                "Type": "Person", 
                "IsFolder": False,
                "Overview": "  \n".join(bio_parts),
                "PremiereDate": premiere_date,
                "ImageTags": {"Primary": p_tag} if perf.get("image_path") else {}, 
                "HasPrimaryImage": bool(perf.get("image_path")),
                "MovieCount": 1, "ChildCount": 1,
                "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"Person-{perf_name}", "ItemId": item_id}
            })
        
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

    def _qp(key, default=""):
        return next((v for k, v in request.query_params.items() if k.lower() == key.lower()), default)

    search_term = _qp("SearchTerm", "").lower()
    name_starts_with = _qp("NameStartsWith", "").lower()
    name_less_than = _qp("NameLessThan", "").lower()
    name_starts_with_or_greater = _qp("NameStartsWithOrGreater", "").lower()
    try: start_index = int(_qp("StartIndex", "0"))
    except: start_index = 0
    try: limit = int(_qp("Limit", "-1"))
    except: limit = -1

    logger.debug(f"Metadata Request -> All {item_type}s (Search Term: '{search_term}')")

    stash_tags = await stash_client.get_all_tags()
    if search_term:
        stash_tags = [t for t in stash_tags if search_term in t.get("name", "").lower()]

    if name_starts_with or name_less_than or name_starts_with_or_greater:
        # Fast Path: Client wants the index count for the alphabet scrollbar (Limit=0)
        if limit == 0:
            count = 0
            for t in stash_tags:
                name = t.get("name", "").lower().strip()
                is_bottom_symbol = not name or not name[0].isalnum()
                if name_less_than:
                    if not is_bottom_symbol and name < name_less_than:
                        count += 1
                elif name_starts_with_or_greater:
                    if is_bottom_symbol or name >= name_starts_with_or_greater:
                        count += 1
                elif name_starts_with:
                    if not is_bottom_symbol and name.startswith(name_starts_with):
                        count += 1
            return JSONResponse({"Items": [], "TotalRecordCount": count, "StartIndex": 0})

        # Slow Path: Client clicked a letter — filter and paginate
        filtered = []
        for t in stash_tags:
            name = t.get("name", "").lower().strip()
            is_bottom_symbol = not name or not name[0].isalnum()
            if name_less_than:
                if not is_bottom_symbol and name < name_less_than:
                    filtered.append(t)
            elif name_starts_with_or_greater:
                if is_bottom_symbol or name >= name_starts_with_or_greater:
                    filtered.append(t)
            elif name_starts_with:
                if not is_bottom_symbol and name.startswith(name_starts_with):
                    filtered.append(t)

        total_count = len(filtered)
        if limit > 0:
            filtered = filtered[start_index : start_index + limit]

        jelly_tags = [{"Name": t.get("name"), "Id": encode_id("tag", str(t.get('id'))), "Type": item_type, "ServerId": server_id, "IsFolder": False} for t in filtered]
        return JSONResponse({"Items": jelly_tags, "TotalRecordCount": total_count, "StartIndex": start_index})

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

async def endpoint_metadata_editor(request: Request):
    """Feeds the Jellyfin Web UI the required layout data for the Edit Metadata screen."""
    return JSONResponse(jellyfin_mapper.get_metadata_editor_info())

async def endpoint_update_item(request: Request):
    """Intercepts Jellyfin UI metadata edits and syncs them to Stash."""
    raw_item_id = request.path_params.get("item_id", "")
    decoded_id = decode_id(raw_item_id)
    
    if not decoded_id.startswith("scene-"):
        return Response(status_code=400)
        
    raw_scene_id = decoded_id.replace("scene-", "")
    
    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"Failed to parse metadata edit payload: {e}")
        return Response(status_code=400)

    stash_update_payload = {"id": raw_scene_id}
    
    # Map basic text fields
    if "Name" in data:
        stash_update_payload["title"] = data["Name"]
    if "Overview" in data:
        stash_update_payload["details"] = data["Overview"]
    
    # Map Jellyfin CriticRating back to Stash rating100
    if "CriticRating" in data:
        try:
            val = int(float(data["CriticRating"]))
            stash_update_payload["rating100"] = max(0, min(100, val))
        except (ValueError, TypeError):
            pass

    # Map Jellyfin CommunityRating back to Stash o_counter
    if "CommunityRating" in data:
        try:
            stash_update_payload["o_counter"] = int(float(data["CommunityRating"]))
        except (ValueError, TypeError):
            pass

    # Format date from Jellyfin ISO to Stash YYYY-MM-DD
    if "PremiereDate" in data:
        if data["PremiereDate"]:
            stash_update_payload["date"] = str(data["PremiereDate"]).split("T")[0]
        else:
            stash_update_payload["date"] = ""

    if "Tags" in data or "Genres" in data:
        raw_tags = data.get("Tags", []) + data.get("Genres", [])
        dynamic_tags_to_ignore = {"recently added", "onot0"}
        
        jellyfin_tags = [
            tag for tag in raw_tags 
            if str(tag).strip().lower() not in dynamic_tags_to_ignore
        ]
        
        logger.debug(f"Syncing {len(jellyfin_tags)} tags for scene {raw_scene_id}")
        
        tag_ids = await stash_client.ensure_tags_exist(jellyfin_tags)
        stash_update_payload["tag_ids"] = tag_ids

    logger.info(f"Applying metadata update to scene {raw_scene_id}...")
    success = await stash_client.update_scene(stash_update_payload)
    
    # Return 204 No Content for a successful Jellyfin save
    return Response(status_code=204 if success else 500)

async def endpoint_item_images_info(request: Request):
    """
    Returns the list of available images for an item. 
    Required by native apps like Fladder to build the Metadata Editor Image tab without crashing.
    """
    return JSONResponse([
        {
            "ImageType": "Primary",
            "ImageIndex": 0
        },
        {
            "ImageType": "Backdrop",
            "ImageIndex": 0
        }
    ])