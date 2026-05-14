import logging
import datetime
import re
from starlette.responses import JSONResponse, Response
from starlette.requests import Request
import config
from core import stash_client, jellyfin_mapper
from core.jellyfin_mapper import encode_id, decode_id, build_folder, generate_image_tag
from core.query_builder import StashQueryBuilder

logger = logging.getLogger(__name__)


def _qp(request: Request, key: str, default: str = "") -> str:
    return next((v for k, v in request.query_params.items() if k.lower() == key.lower()), default)


# ---------------------------------------------------------------------------
# Item detail handlers
# ---------------------------------------------------------------------------

async def _handle_nav_folder_details(decoded_id: str, item_id: str, server_id: str, cache_version: int) -> JSONResponse:
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
            "root-alltags": "All Tags",
        }
        item_name = root_map.get(clean_dec, "Folder")
    elif clean_dec.startswith("tag-"):
        raw_id = clean_dec.replace("tag-", "")
        all_tags = await stash_client.get_all_tags()
        match = next((t for t in all_tags if str(t.get("id")) == raw_id), None)
        if match:
            item_name = match.get("name", "Folder")
    elif clean_dec.startswith("filter-"):
        raw_id = clean_dec.replace("filter-", "")
        filters = await stash_client.get_saved_filters()
        match = next((f for f in filters if str(f.get("id")) == raw_id), None)
        if match:
            item_name = match.get("name", "Folder")

    is_collection = is_root and not is_nav_folder
    return JSONResponse(build_folder(item_name, item_id, server_id, cache_version, is_collection))


async def _handle_studio_details(decoded_id: str, server_id: str, cache_version: int) -> JSONResponse:
    raw_id = decoded_id.replace("studio-", "")
    safe_id = encode_id("studio", raw_id)

    logger.debug(f"RICH STUDIO HIT: Jellyfin requested studio ID '{raw_id}'. Querying Stash...")

    query = "query($id: ID!) { findStudio(id: $id) { name details image_path url parent_studio { name } } }"
    data = await stash_client.call_graphql(query, {"id": raw_id})
    studio = data.get("findStudio") if data else None

    logger.debug(f"RICH STUDIO RESULT: {studio}")

    if not studio:
        return JSONResponse({"Name": "Studio", "SortName": "Studio", "Id": safe_id, "ServerId": server_id, "Type": "BoxSet", "IsFolder": True})

    s_tag = generate_image_tag("studio", raw_id, cache_version)
    studio_name = studio.get("name", "Unknown Studio")

    overview_parts = []
    if studio.get("parent_studio"):
        overview_parts.append(f"Parent Studio: {studio['parent_studio']['name']}")
    if studio.get("url"):
        overview_parts.append(f"Website: {studio['url']}")
    if studio.get("details"):
        overview_parts.append(f"\n{studio['details']}")

    return JSONResponse({
        "Name": studio_name,
        "SortName": studio_name,
        "Id": safe_id,
        "ServerId": server_id,
        "Type": "BoxSet",
        "IsFolder": True,
        "Overview": "\n".join(overview_parts),
        "ImageTags": {"Primary": s_tag} if studio.get("image_path") else {},
        "HasPrimaryImage": bool(studio.get("image_path")),
    })


def _build_performer_bio(perf: dict) -> str:
    parts = []
    if perf.get("alias_list"):
        parts.append(f"Aliases: {', '.join(perf['alias_list'])}")
    if perf.get("gender"):
        parts.append(f"Gender: {perf['gender']}")
    if perf.get("career_length"):
        parts.append(f"Career Length: {perf['career_length']}")
    if perf.get("country"):
        parts.append(f"Country: {perf['country']}")
    if perf.get("ethnicity"):
        parts.append(f"Ethnicity: {perf['ethnicity']}")
    if perf.get("hair_color"):
        parts.append(f"Hair: {perf['hair_color']}")
    if perf.get("eye_color"):
        parts.append(f"Eyes: {perf['eye_color']}")

    height_cm = perf.get("height_cm")
    if height_cm:
        try:
            total_inches = round(int(height_cm) / 2.54)
            ft, inch = divmod(total_inches, 12)
            parts.append(f"Height: {height_cm} cm ({ft}' {inch}\")")
        except ValueError:
            parts.append(f"Height: {height_cm} cm")

    if perf.get("weight"):
        parts.append(f"Weight: {perf['weight']} kg")
    if perf.get("measurements"):
        parts.append(f"Measurements: {perf['measurements']}")
    if perf.get("fake_tits"):
        parts.append(f"Fake Tits: {perf['fake_tits']}")

    penis_cm = perf.get("penis_length")
    if penis_cm:
        try:
            p_inches = round(float(penis_cm) / 2.54, 1)
            parts.append(f"Penis Length: {penis_cm} cm ({p_inches}\")")
        except ValueError:
            parts.append(f"Penis Length: {penis_cm} cm")

    if perf.get("circumcised"):
        parts.append(f"Circumcised: {str(perf['circumcised']).capitalize()}")
    if perf.get("piercings"):
        parts.append(f"Piercings: {perf['piercings']}")
    if perf.get("tattoos"):
        parts.append(f"Tattoos: {perf['tattoos']}")
    if perf.get("details"):
        parts.append(f"\n\n{perf['details']}")

    return "  \n".join(parts)


async def _handle_performer_details(decoded_id: str, item_id: str, server_id: str) -> JSONResponse:
    raw_id = decoded_id.replace("person-", "")

    logger.debug(f"RICH CAST HIT: Jellyfin requested person ID '{raw_id}'. Querying Stash...")

    perf = await stash_client.get_performer(raw_id)

    logger.debug(f"RICH CAST RESULT: {perf}")

    if not perf:
        return Response(status_code=404)

    cache_version = getattr(config, "CACHE_VERSION", 0)
    p_tag = generate_image_tag("person", raw_id, cache_version)
    perf_name = perf.get("name", "Unknown Person")
    premiere_date = f"{perf['birthdate']}T00:00:00.0000000Z" if perf.get("birthdate") else None

    return JSONResponse({
        "Name": perf_name,
        "SortName": perf_name,
        "Id": item_id,
        "ServerId": server_id,
        "Type": "Person",
        "IsFolder": False,
        "Overview": _build_performer_bio(perf),
        "PremiereDate": premiere_date,
        "ImageTags": {"Primary": p_tag} if perf.get("image_path") else {},
        "HasPrimaryImage": bool(perf.get("image_path")),
        "MovieCount": 1,
        "ChildCount": 1,
        "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": f"Person-{perf_name}", "ItemId": item_id},
    })


async def _handle_scene_details(decoded_id: str) -> JSONResponse:
    number_match = re.search(r'\d+', decoded_id)
    if not number_match:
        logger.warning(f"Invalid ID format requested: {decoded_id}")
        return JSONResponse({"error": f"Invalid ID format: {decoded_id}"}, status_code=400)

    scene = await stash_client.get_scene(number_match.group())
    if scene:
        return JSONResponse(jellyfin_mapper.format_jellyfin_item(scene))

    logger.debug(f"Item not found in Stash: {decoded_id}")
    return JSONResponse({"error": "Item not found"}, status_code=404)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

async def endpoint_item_details(request: Request):
    item_id = request.path_params.get("item_id", "")
    decoded_id = decode_id(item_id)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    cache_version = getattr(config, "CACHE_VERSION", 0)

    logger.debug(f"Metadata Request -> Item Details for Decoded ID: {decoded_id}")

    # Live TV channels and programs are identified by their encoded IDs
    from api import live_tv_routes
    ch = await live_tv_routes.get_channel_by_jellyfin_id(item_id)
    if ch is not None:
        return JSONResponse(live_tv_routes._channel_to_jellyfin(ch, server_id, item_id))
    prog = await live_tv_routes.get_program_by_jellyfin_id(item_id)
    if prog is not None:
        channels = await live_tv_routes._get_channels()
        channels_by_tvg_id = {c["tvg_id"]: c for c in channels}
        return JSONResponse(live_tv_routes._program_to_jellyfin(prog, server_id, channels_by_tvg_id, item_id))

    if "root-" in decoded_id or "tag-" in decoded_id or "filter-" in decoded_id:
        return await _handle_nav_folder_details(decoded_id, item_id, server_id, cache_version)
    if decoded_id.startswith("studio-"):
        return await _handle_studio_details(decoded_id, server_id, cache_version)
    if decoded_id.startswith("person-"):
        return await _handle_performer_details(decoded_id, item_id, server_id)
    return await _handle_scene_details(decoded_id)


async def endpoint_tags(request: Request):
    item_type = "Genre" if "genre" in request.url.path.lower() else "Tag"
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    search_term = _qp(request, "SearchTerm").lower()
    name_starts_with = _qp(request, "NameStartsWith").lower()
    name_less_than = _qp(request, "NameLessThan").lower()
    name_starts_with_or_greater = _qp(request, "NameStartsWithOrGreater").lower()
    try:
        start_index = int(_qp(request, "StartIndex", "0"))
    except ValueError:
        start_index = 0
    try:
        limit = int(_qp(request, "Limit", "-1"))
    except ValueError:
        limit = -1

    logger.debug(f"Metadata Request -> All {item_type}s (Search Term: '{search_term}')")

    stash_tags = await stash_client.get_all_tags()
    if search_term:
        stash_tags = [t for t in stash_tags if search_term in t.get("name", "").lower()]

    if name_starts_with or name_less_than or name_starts_with_or_greater:
        def _matches(t: dict) -> bool:
            name = t.get("name", "").lower().strip()
            is_symbol = not name or not name[0].isalnum()
            if name_less_than:
                return not is_symbol and name < name_less_than
            if name_starts_with_or_greater:
                return is_symbol or name >= name_starts_with_or_greater
            return not is_symbol and name.startswith(name_starts_with)

        if limit == 0:
            count = sum(1 for t in stash_tags if _matches(t))
            return JSONResponse({"Items": [], "TotalRecordCount": count, "StartIndex": 0})

        filtered = [t for t in stash_tags if _matches(t)]
        total_count = len(filtered)
        if limit > 0:
            filtered = filtered[start_index : start_index + limit]
        jelly_tags = [{"Name": t.get("name"), "Id": encode_id("tag", str(t.get("id"))), "Type": item_type, "ServerId": server_id, "IsFolder": False} for t in filtered]
        return JSONResponse({"Items": jelly_tags, "TotalRecordCount": total_count, "StartIndex": start_index})

    jelly_tags = [{"Name": t.get("name"), "Id": encode_id("tag", str(t.get("id"))), "Type": item_type, "ServerId": server_id, "IsFolder": False} for t in stash_tags]
    return JSONResponse({"Items": jelly_tags, "TotalRecordCount": len(jelly_tags), "StartIndex": 0})


async def endpoint_years(request: Request):
    logger.debug("Metadata Request -> Years List")
    current_year = datetime.datetime.now().year
    server_id = getattr(config, "SERVER_ID", "stash-proxy")
    years = [{"Name": str(y), "Id": encode_id("year", str(y)), "Type": "Year", "ProductionYear": y, "ServerId": server_id, "IsFolder": False} for y in range(current_year, 1989, -1)]
    return JSONResponse({"Items": years, "TotalRecordCount": len(years), "StartIndex": 0})


async def endpoint_studios(request: Request):
    parent_id = _qp(request, "parentid")
    decoded_parent = decode_id(parent_id) if parent_id else None
    search_term = _qp(request, "searchterm").lower()
    cache_version = getattr(config, "CACHE_VERSION", 0)
    server_id = getattr(config, "SERVER_ID", "stash-proxy")

    logger.debug(f"Metadata Request -> Studios List (library: {decoded_parent or 'all'})")

    builder = StashQueryBuilder(request, {"decoded_parent_id": decoded_parent})
    _, scene_filter, _, _ = await builder.build()

    if not scene_filter:
        studios = await stash_client.get_all_studios()
    else:
        studios = await stash_client.fetch_studios_in_filter(scene_filter)

    if search_term:
        studios = [s for s in studios if search_term in (s.get("name") or "").lower()]

    jelly_studios = [{
        "Name": s.get("name"), "Id": encode_id("studio", str(s.get("id"))), "Type": "Studio", "ServerId": server_id, "IsFolder": False,
        "ImageTags": {"Primary": generate_image_tag("studio", str(s.get("id")), cache_version)}, "HasPrimaryImage": bool(s.get("image_path")),
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

    if "Name" in data:
        stash_update_payload["title"] = data["Name"]
    if "Overview" in data:
        stash_update_payload["details"] = data["Overview"]

    if "CriticRating" in data:
        try:
            stash_update_payload["rating100"] = max(0, min(100, int(float(data["CriticRating"]))))
        except (ValueError, TypeError):
            pass

    if "CommunityRating" in data:
        try:
            stash_update_payload["o_counter"] = int(float(data["CommunityRating"]))
        except (ValueError, TypeError):
            pass

    if "PremiereDate" in data:
        stash_update_payload["date"] = str(data["PremiereDate"]).split("T")[0] if data["PremiereDate"] else ""

    if "Tags" in data or "Genres" in data:
        dynamic_tags_to_ignore = {"recently added", "onot0"}
        jellyfin_tags = [
            tag for tag in data.get("Tags", []) + data.get("Genres", [])
            if str(tag).strip().lower() not in dynamic_tags_to_ignore
        ]
        logger.debug(f"Syncing {len(jellyfin_tags)} tags for scene {raw_scene_id}")
        stash_update_payload["tag_ids"] = await stash_client.ensure_tags_exist(jellyfin_tags)

    logger.info(f"Applying metadata update to scene {raw_scene_id}...")
    success = await stash_client.update_scene(stash_update_payload)
    return Response(status_code=204 if success else 500)


async def endpoint_item_images_info(request: Request):
    """
    Returns the list of available images for an item.
    Required by native apps like Fladder to build the Metadata Editor Image tab without crashing.
    """
    return JSONResponse([
        {"ImageType": "Primary", "ImageIndex": 0},
        {"ImageType": "Backdrop", "ImageIndex": 0},
    ])
