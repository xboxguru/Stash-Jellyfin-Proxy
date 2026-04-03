import os
import datetime
import hashlib
import logging
from typing import Dict, Any
import config

logger = logging.getLogger(__name__)

# --- REFACTORED HELPERS (DRY) ---
def generate_image_tag(prefix: str, raw_id: str, cache_version: int) -> str:
    """Responsibility: Centrally generate cache-busting image tags for all entities."""
    return hashlib.md5(f"{prefix}-{raw_id}-v{cache_version}".encode()).hexdigest()

def build_folder(name: str, folder_id: str, server_id: str, cache_version: int, is_collection: bool = False, collection_type: str = "movies", is_user_view: bool = False) -> dict:
    """Responsibility: Standardize the creation of Jellyfin virtual folders across all routes."""
    logo_hash = generate_image_tag("logo", "proxy", cache_version)
    
    folder = {
        "Name": name, 
        "SortName": name, 
        "Id": folder_id, 
        "Etag": logo_hash, # <-- FIX: Added Etag for Tunarr
        "DisplayPreferencesId": folder_id,
        "ServerId": server_id, 
        "Type": "UserView" if is_user_view else ("CollectionFolder" if is_collection else "Folder"), 
        "IsFolder": True,
        "PrimaryImageAspectRatio": 1.7777777777777777,
        "ImageTags": {"Primary": logo_hash, "Thumb": logo_hash},
        "HasPrimaryImage": True, "HasThumb": True, "HasBackdrop": True, 
        "BackdropImageTags": [logo_hash]
    }
    
    if is_collection or is_user_view: 
        folder["CollectionType"] = collection_type
    if is_user_view:
        folder.update({
            "ItemId": folder_id,
            "ChannelId": None,
            "UserData": {"PlaybackPositionTicks": 0, "PlayCount": 0, "IsFavorite": False, "Played": False, "Key": hyphens(folder_id), "ItemId": folder_id},
            "LibraryOptions": {"PathInfos": []}, "Locations": [],
            "ImageBlurHashes": {}, "LocationType": "FileSystem", "MediaType": "Unknown"
        })
        
    return folder

def encode_id(prefix: str, raw_id: str) -> str:
    s = f"{prefix}-{raw_id}"
    hex_str = s.encode('utf-8').hex()
    if len(hex_str) > 32: return hashlib.md5(s.encode('utf-8')).hexdigest()
    return hex_str.ljust(32, '0')

def decode_id(encoded_id: str) -> str:
    clean_id = encoded_id.replace("-", "")
    if clean_id.startswith("scene") or clean_id.startswith("person") or clean_id.startswith("studio"): return encoded_id 
    try:
        decoded_str = bytes.fromhex(clean_id).decode('utf-8').replace("\x00", "").strip()
        if any(prefix in decoded_str for prefix in ["scene-", "person-", "studio-", "tag-", "root-", "filter-", "year-"]):
            return decoded_str
    except Exception: pass
    return encoded_id

def hyphens(h: str) -> str:
    if len(h) != 32: return h
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

def _build_trickplay_dict(item_id: str, runtime_ticks: int, files: list) -> dict:
    if runtime_ticks <= 0 or not files: return {}
    video_width = int(files[0].get("width") or 1920)
    video_height = int(files[0].get("height") or 1080)
    aspect_ratio = video_width / video_height if video_height > 0 else 1.777
    actual_width = 160
    actual_height = int(actual_width / aspect_ratio)
    thumbnail_count = 81
    interval_ms = int((runtime_ticks / 10000) / thumbnail_count)
    return {item_id: {str(actual_width): {"Width": actual_width, "Height": actual_height, "TileWidth": 9, "TileHeight": 9, "ThumbnailCount": thumbnail_count, "Interval": interval_ms, "Bandwidth": 0}}}

def _build_media_sources(item_id: str, path: str, files: list, runtime_ticks: int, title: str) -> list:
    if not path or not files: return []
    file_data = files[0]
    v_codec = str(file_data.get("video_codec") or "h264").lower()
    a_codec = str(file_data.get("audio_codec") or "aac").lower()
    container = str(file_data.get("format") or "mp4").lower()
    
    needs_transcode = v_codec not in ["h264", "h265", "hevc", "avc", "vp8", "vp9", "av1"] or container not in ["mp4", "m4v", "mov", "webm"]
    base_stream_flags = {"IsInterlaced": False, "IsDefault": True, "IsForced": False, "IsHearingImpaired": False, "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False}
    
    video_stream = {**base_stream_flags, "Codec": v_codec, "Type": "Video", "Width": file_data.get("width") or 0, "Height": file_data.get("height") or 0, "Index": 0, "BitRate": file_data.get("bit_rate") or 0, "IsAVC": v_codec in ["h264", "avc"]}
    audio_stream = {**base_stream_flags, "Codec": a_codec, "Type": "Audio", "Index": 1, "Channels": 2}

    media_source = {
        "Id": item_id, "Path": path, "Protocol": "File", "Type": "Default", "Container": container,
        "RunTimeTicks": runtime_ticks, "IsRemote": False, "SupportsTranscoding": True, "VideoType": "VideoFile",
        "MediaStreams": [video_stream, audio_stream], "MediaAttachments": [], "Formats": [], "RequiredHttpHeaders": {},
        "Name": title, "Size": int(file_data.get("size") or 0), "ReadAtNativeFramerate": False, "SupportsProbing": True,
        "IgnoreDts": False, "IgnoreIndex": False, "GenPtsInput": False, "IsInfiniteStream": False, "RequiresOpening": False, "RequiresClosing": False, "RequiresLooping": False, "HasSegments": False
    }

    if needs_transcode:
        media_source.update({"SupportsDirectPlay": False, "SupportsDirectStream": False, "TranscodingUrl": f"/Videos/{item_id}/master.m3u8", "TranscodingSubProtocol": "hls", "TranscodingContainer": "ts"})
    else:
        media_source.update({"SupportsDirectPlay": True, "SupportsDirectStream": True, "DirectStreamUrl": f"/Videos/{item_id}/stream", "TranscodingSubProtocol": "http"})
    return [media_source]

def _build_people(performers: list, cache_version: int, fake_blurhash: str) -> list:
    people_list = []
    for p in performers:
        if p.get("name") and p.get("id"):
            person = {"Name": p.get("name"), "Type": "Actor", "Role": "Actor", "Id": encode_id("person", str(p["id"])), "ImageBlurHashes": {}}
            if p.get("image_path"):
                p_tag = generate_image_tag("person", p['id'], cache_version)
                person["PrimaryImageTag"] = p_tag
                person["ImageBlurHashes"] = {"Primary": {p_tag: fake_blurhash}}
            people_list.append(person)
    return people_list

def _build_dates(date_str: str, created_at_str: str, now_iso: str, recent_days_limit: int) -> dict:
    result = {"_is_recent": False}
    if created_at_str:
        base_time = created_at_str.replace("Z", "").replace(" ", "T")[:19]
        formatted_created = f"{base_time}.0000000Z"
        if recent_days_limit > 0:
            try:
                dt = datetime.datetime.strptime(base_time, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
                if (datetime.datetime.now(datetime.timezone.utc) - dt).days <= recent_days_limit: result["_is_recent"] = True
            except Exception: pass
    else: formatted_created = now_iso

    result["DateCreated"] = formatted_created
    if date_str and len(date_str) >= 4:
        try:
            result["ProductionYear"] = int(date_str[:4])
            clean_date = f"{date_str}-01-01" if len(date_str) == 4 else f"{date_str}-01" if len(date_str) == 7 else date_str
            result["PremiereDate"] = f"{clean_date}T00:00:00.0000000Z"
        except:
            result["PremiereDate"] = formatted_created
            result["ProductionYear"] = int(formatted_created[:4])
    else:
        result["PremiereDate"] = formatted_created
        result["ProductionYear"] = int(formatted_created[:4])
    return result

def _build_studios(studio_obj: dict, cache_version: int, fake_blurhash: str) -> list:
    if not studio_obj or not studio_obj.get("name"): return []
    studio_id = studio_obj.get("id")
    studio_item = {"Name": studio_obj.get("name"), "Id": encode_id("studio", str(studio_id)), "ImageBlurHashes": {}}
    if studio_obj.get("image_path"):
        s_tag = generate_image_tag("studio", studio_id, cache_version)
        studio_item.update({"PrimaryImageTag": s_tag, "ImageTags": {"Primary": s_tag}, "ImageBlurHashes": {"Primary": {s_tag: fake_blurhash}}})
    return [studio_item]

def format_jellyfin_item(scene: Dict[str, Any], parent_id: str = None) -> Dict[str, Any]:
    raw_id = str(scene.get("id"))
    item_id = encode_id("scene", raw_id)
    cache_version = getattr(config, "CACHE_VERSION", 0)
    fake_blurhash = "LKO2?U%2Tw=w]~RBVZRi};RPxuwH"
    files = scene.get("files") or []
    
    path = (files[0].get("path") or "").replace("\\", "/") if files else ""
    runtime_ticks = int((files[0].get("duration") or 0) * 10000000) if files else 0
    title = scene.get("title") or scene.get("code") or (os.path.splitext(os.path.basename(path))[0] if path else f"Scene {raw_id}")
    
    primary_tag = generate_image_tag("scene", raw_id, cache_version)
    backdrop_tag = generate_image_tag("backdrop", raw_id, cache_version)

    item = {
        "Name": title, 
        "SortName": title, 
        "Id": item_id, 
        "Etag": primary_tag,
        "OfficialRating": "XXX",
        "CommunityRating": scene.get("o_counter"),
        "CriticRating": scene.get("rating100"),
        "ServerId": getattr(config, "SERVER_ID", "stash-proxy"),
        "Type": "Movie", "IsFolder": False, "MediaType": "Video", "ParentId": parent_id if parent_id else encode_id("root", "scenes"),
        "LocationType": "FileSystem",
        "LockedFields": [],
        "LockData": False,
        "CanDelete": True,
        "CanDownload": True,
        "HasPrimaryImage": True, "HasBackdrop": True, "ImageTags": {"Primary": primary_tag, "Thumb": primary_tag}, 
        "PrimaryImageAspectRatio": 1.777, "BackdropImageTags": [backdrop_tag],
        "ImageBlurHashes": {"Primary": {primary_tag: fake_blurhash}, "Thumb": {primary_tag: fake_blurhash}, "Backdrop": {backdrop_tag: fake_blurhash}},
        "RunTimeTicks": runtime_ticks, "Width": (files[0].get("width") or 0) if files else 0, "Height": (files[0].get("height") or 0) if files else 0,
        "Trickplay": _build_trickplay_dict(item_id, runtime_ticks, files),
        "MediaSources": _build_media_sources(item_id, path, files, runtime_ticks, title),
        "People": _build_people(scene.get("performers") or [], cache_version, fake_blurhash),
        "Studios": _build_studios(scene.get("studio"), cache_version, fake_blurhash),
        "UserData": {
            "PlaybackPositionTicks": int((scene.get("resume_time") or 0) * 10000000),
            "PlayCount": scene.get("play_count") or 0,
            "IsFavorite": ((scene.get("rating100") or 0) > 0) if getattr(config, "FAVORITE_ACTION", "o_counter").lower() == "rating" else ((scene.get("o_counter") or 0) > 0),
            "Played": (scene.get("play_count") or 0) > 0, "Key": hyphens(item_id), "ItemId": item_id
        }
    }
    
    # --- NEW: Map Stash Markers to Jellyfin Chapters (Path B - Router) ---
    try:
        chapters = []
        seen_ticks = set()
        
        # 1. 0-tick starting chapter (Maps to image_index = 0)
        chapters.append({
            "StartPositionTicks": 0,
            "Name": "Start",
            "ImageTag": generate_image_tag("chapter", f"{raw_id}_0", cache_version),
            "ImageDateModified": "0001-01-01T00:00:00.0000000Z"
        })
        seen_ticks.add(0)
        
        # 2. Sort markers FIRST so array indexes perfectly match Jellyfin requests
        markers = scene.get("scene_markers") or []
        markers.sort(key=lambda x: float(x.get("seconds", 0)))
        
        for idx, marker in enumerate(markers):
            raw_seconds = marker.get("seconds")
            if raw_seconds is None: 
                continue
                
            seconds = float(raw_seconds)
            ticks = int(seconds * 10000000)
            
            if ticks in seen_ticks: 
                continue
            seen_ticks.add(ticks)
            
            marker_title = marker.get("title")
            if not marker_title and marker.get("primary_tag"):
                marker_title = marker["primary_tag"].get("name")
                
            final_title = str(marker_title).strip() if marker_title else f"Chapter {len(chapters)}"
            
            # Use idx + 1 because 0 is the "Start" chapter
            chapter_img_tag = generate_image_tag("chapter", f"{raw_id}_{idx+1}", cache_version)
            
            chapters.append({
                "StartPositionTicks": ticks,
                "Name": final_title,
                "ImageTag": chapter_img_tag,
                "ImageDateModified": "0001-01-01T00:00:00.0000000Z"
            })
            
        if len(chapters) > 1:
            item["Chapters"] = chapters
            
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Failed to parse markers for scene {raw_id}: {e}")
    # ------------------------------------------------------------
    
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    dates_info = _build_dates(scene.get("date"), scene.get("created_at"), now_iso, getattr(config, "RECENT_DAYS", 14))
    
    item_tags = [t.get("name") for t in scene.get("tags") or [] if t.get("name")]
    if dates_info.pop("_is_recent", False): item_tags.append("Recently Added")
    if (scene.get("o_counter") or 0) >= 1: item_tags.append("Onot0")
    
    item["Tags"] = item_tags
    item["Genres"] = item_tags[:10]
    item.update(dates_info)

    overview_parts = [scene.get("details", "")] if scene.get("details") else []
    if scene.get("studio") and scene["studio"].get("name"): overview_parts.append(f"Studio: {scene['studio']['name']}")
    item["Overview"] = "\n\n".join(overview_parts)

    return item

def get_metadata_editor_info() -> Dict[str, Any]:
    """Responsibility: Provide the static layout options for the Jellyfin Metadata Editor UI."""
    return {
        "ParentalRatingOptions": [
            {"Name": "Unrated", "Value": 0},
            {"Name": "PG-13", "Value": 13},
            {"Name": "R", "Value": 17},
            {"Name": "XXX", "Value": 1000}
        ],
        "Countries": [
            {"Name": "US", "DisplayName": "United States", "TwoLetterISORegionName": "US", "ThreeLetterISORegionName": "USA"},
            {"Name": "JP", "DisplayName": "Japan", "TwoLetterISORegionName": "JP", "ThreeLetterISORegionName": "JPN"}
        ],
        "Cultures": [
            {"Name": "English", "DisplayName": "English", "TwoLetterISOLanguageName": "en", "ThreeLetterISOLanguageName": "eng"}
        ],
        "ExternalIdInfos": [
            {"Name": "Stash", "Key": "Stash", "Type": "Movie"}
        ],
        "ContentTypeOptions": []
    }