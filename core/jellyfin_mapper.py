import os
import datetime
import hashlib
import logging
from typing import Dict, Any
import config

logger = logging.getLogger(__name__)

def encode_id(prefix: str, raw_id: str) -> str:
    """Encodes a string into a strict 32-character hex UUID for Findroid."""
    s = f"{prefix}-{raw_id}"
    hex_str = s.encode('utf-8').hex()
    if len(hex_str) > 32:
        return hashlib.md5(s.encode('utf-8')).hexdigest()
    return hex_str.ljust(32, '0')

def decode_id(encoded_id: str) -> str:
    """Decodes the 32-character hex UUID back into our proxy ID format."""
    clean_id = encoded_id.replace("-", "")
    
    if clean_id.startswith("scene") or clean_id.startswith("person") or clean_id.startswith("studio"):
        return encoded_id 
        
    try:
        decoded_bytes = bytes.fromhex(clean_id)
        # FIX: Violently scrub ALL null bytes and whitespace to prevent ID mismatch bugs globally!
        decoded_str = decoded_bytes.decode('utf-8').replace("\x00", "").strip()
        
        if "scene-" in decoded_str or "person-" in decoded_str or "studio-" in decoded_str or "tag-" in decoded_str or "root-" in decoded_str or "filter-" in decoded_str or "year-" in decoded_str:
            return decoded_str
    except Exception:
        pass
        
    return encoded_id

def hyphens(h: str) -> str:
    if len(h) != 32: return h
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

def _build_trickplay_dict(item_id: str, runtime_ticks: int, files: list) -> dict:
    """Calculates BIF/Trickplay thumbnail intervals for video scrubbing."""
    if runtime_ticks <= 0 or not files:
        return {}
        
    # FIX: Safely fallback if Stash explicitly returns `null` for unprobed files
    video_width = files[0].get("width")
    video_height = files[0].get("height")
    
    video_width = int(video_width) if video_width else 1920
    video_height = int(video_height) if video_height else 1080
    
    aspect_ratio = video_width / video_height if video_height > 0 else 1.777
    
    actual_width = 160
    actual_height = int(actual_width / aspect_ratio)
    
    thumbnail_count = 81
    interval_ms = int((runtime_ticks / 10000) / thumbnail_count)
    
    return {
        item_id: {
            str(actual_width): {  
                "Width": actual_width, "Height": actual_height,
                "TileWidth": 9, "TileHeight": 9,
                "ThumbnailCount": thumbnail_count, "Interval": interval_ms, "Bandwidth": 0
            }
        }
    }

def _build_media_sources(item_id: str, path: str, files: list, runtime_ticks: int, title: str) -> list:
    """Analyzes codecs and builds the direct-play or HLS transcode profile."""
    if not path or not files:
        return []
        
    file_data = files[0]
    v_codec = str(file_data.get("video_codec") or "h264").lower()
    a_codec = str(file_data.get("audio_codec") or "aac").lower()
    container = str(file_data.get("format") or "mp4").lower()
    
    safe_codecs = ["h264", "h265", "hevc", "avc", "vp8", "vp9", "av1"]
    safe_containers = ["mp4", "m4v", "mov", "webm"]
    needs_transcode = v_codec not in safe_codecs or container not in safe_containers

    base_stream_flags = {"IsInterlaced": False, "IsDefault": True, "IsForced": False, "IsHearingImpaired": False, "IsExternal": False, "IsTextSubtitleStream": False, "SupportsExternalStream": False}
    
    video_stream = {
        **base_stream_flags, 
        "Codec": v_codec, 
        "Type": "Video", 
        "Width": file_data.get("width") or 0, 
        "Height": file_data.get("height") or 0, 
        "Index": 0, 
        "BitRate": file_data.get("bit_rate") or 0, 
        "IsAVC": v_codec in ["h264", "avc"]
    }
    audio_stream = {**base_stream_flags, "Codec": a_codec, "Type": "Audio", "Index": 1, "Channels": 2}

    media_source = {
        "Id": item_id, "Path": path, "Protocol": "File", "Type": "Default", "Container": container,
        "RunTimeTicks": runtime_ticks, "IsRemote": False, "SupportsTranscoding": True, "VideoType": "VideoFile",
        "MediaStreams": [video_stream, audio_stream], "MediaAttachments": [], "Formats": [], "RequiredHttpHeaders": {},
        "Name": title, "Size": int(file_data.get("size") or 0), "ReadAtNativeFramerate": False, "SupportsProbing": True,
        
        # --- NEW: KOTLIN SDK REQUIRED FIELDS ---
        "IgnoreDts": False, 
        "IgnoreIndex": False, 
        "GenPtsInput": False, 
        "IsInfiniteStream": False, 
        "RequiresOpening": False, 
        "RequiresClosing": False, 
        "RequiresLooping": False, 
        "HasSegments": False
    }

    if needs_transcode:
        media_source.update({
            "SupportsDirectPlay": False, "SupportsDirectStream": False,
            "TranscodingUrl": f"/Videos/{item_id}/master.m3u8",
            "TranscodingSubProtocol": "hls", "TranscodingContainer": "ts"
        })
    else:
        media_source.update({
            "SupportsDirectPlay": True, "SupportsDirectStream": True,
            "DirectStreamUrl": f"/Videos/{item_id}/stream",
            "TranscodingSubProtocol": "http"
        })

    return [media_source]

def _build_people(performers: list, cache_version: int, fake_blurhash: str) -> list:
    """Formats Stash performers into Jellyfin actors."""
    people_list = []
    for p in performers:
        if p.get("name") and p.get("id"):
            person = {"Name": p.get("name"), "Type": "Actor", "Role": "Actor", "Id": encode_id("person", str(p["id"])), "ImageBlurHashes": {}}
            if p.get("image_path"):
                p_tag = hashlib.md5(f"person-{p['id']}-v{cache_version}".encode()).hexdigest()
                person["PrimaryImageTag"] = p_tag
                person["ImageBlurHashes"] = {"Primary": {p_tag: fake_blurhash}}
            people_list.append(person)
    return people_list

def _build_dates(date_str: str, created_at_str: str, now_iso: str, recent_days_limit: int) -> dict:
    """Extracts, formats, and safely parses Stash dates into Jellyfin timestamps."""
    result = {"_is_recent": False}
    
    if created_at_str:
        base_time = created_at_str.replace("Z", "").replace(" ", "T")[:19]
        formatted_created = f"{base_time}.0000000Z"
        if recent_days_limit > 0:
            try:
                dt = datetime.datetime.strptime(base_time, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
                if (datetime.datetime.now(datetime.timezone.utc) - dt).days <= recent_days_limit:
                    result["_is_recent"] = True
            except Exception: pass
    else:
        formatted_created = now_iso

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
    """Safely extracts Studio objects and their associated image hashes."""
    if not studio_obj or not studio_obj.get("name"):
        return []

    studio_id = studio_obj.get("id")
    has_studio_image = bool(studio_obj.get("image_path"))
    
    studio_item = {
        "Name": studio_obj.get("name"),
        "Id": encode_id("studio", str(studio_id)),
        "ImageBlurHashes": {}
    }
    
    if has_studio_image:
        s_tag = hashlib.md5(f"studio-{studio_id}-v{cache_version}".encode()).hexdigest()
        studio_item["PrimaryImageTag"] = s_tag
        studio_item["ImageTags"] = {"Primary": s_tag}
        studio_item["ImageBlurHashes"] = {"Primary": {s_tag: fake_blurhash}}
        
    return [studio_item]

def format_jellyfin_item(scene: Dict[str, Any], parent_id: str = None) -> Dict[str, Any]:
    """Transforms a Stash Scene object into a Jellyfin Movie/Video object."""
    raw_id = str(scene.get("id"))
    item_id = encode_id("scene", raw_id)
    cache_version = getattr(config, "CACHE_VERSION", 0)
    fake_blurhash = "LKO2?U%2Tw=w]~RBVZRi};RPxuwH"
    
    files = scene.get("files") or []
    
    # FIX 1: Safely handle if path is explicitly null
    path = (files[0].get("path") or "").replace("\\", "/") if files else ""
    runtime_ticks = int((files[0].get("duration") or 0) * 10000000) if files else 0
    
    title = scene.get("title") or scene.get("code") or (os.path.splitext(os.path.basename(path))[0] if path else f"Scene {raw_id}")
    
    primary_tag = hashlib.md5(f"scene-{raw_id}-v{cache_version}".encode()).hexdigest()
    backdrop_tag = hashlib.md5(f"backdrop-{raw_id}-v{cache_version}".encode()).hexdigest()

    item = {
        "Name": title,
        "SortName": title,
        "Id": item_id,
        "ServerId": getattr(config, "SERVER_ID", "stash-proxy"),
        "Type": "Movie",
        "IsFolder": False,
        "MediaType": "Video",
        "ParentId": parent_id if parent_id else encode_id("root", "scenes"),
        
        "HasPrimaryImage": True,
        "HasBackdrop": True,
        "ImageTags": {"Primary": primary_tag, "Thumb": primary_tag}, 
        "PrimaryImageAspectRatio": 1.777,
        "BackdropImageTags": [backdrop_tag],
        "ImageBlurHashes": {"Primary": {primary_tag: fake_blurhash}, "Thumb": {primary_tag: fake_blurhash}, "Backdrop": {backdrop_tag: fake_blurhash}},
        
        "RunTimeTicks": runtime_ticks,
        "Width": (files[0].get("width") or 0) if files else 0,
        "Height": (files[0].get("height") or 0) if files else 0,
        
        # --- DELEGATED TO HELPERS ---
        "Trickplay": _build_trickplay_dict(item_id, runtime_ticks, files),
        "MediaSources": _build_media_sources(item_id, path, files, runtime_ticks, title),
        "People": _build_people(scene.get("performers") or [], cache_version, fake_blurhash),
        "Studios": _build_studios(scene.get("studio"), cache_version, fake_blurhash),
        
        "UserData": {
            "PlaybackPositionTicks": int((scene.get("resume_time") or 0) * 10000000),
            "PlayCount": scene.get("play_count") or 0,
            "IsFavorite": ((scene.get("rating100") or 0) > 0) if getattr(config, "FAVORITE_ACTION", "o_counter").lower() == "rating" else ((scene.get("o_counter") or 0) > 0),
            "Played": (scene.get("play_count") or 0) > 0,
            "Key": hyphens(item_id),
            "ItemId": item_id
        }
    }
    
    # Merge Date dictionary and evaluate "Recently Added" status
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
    dates_info = _build_dates(scene.get("date"), scene.get("created_at"), now_iso, getattr(config, "RECENT_DAYS", 14))
    
    item_tags = [t.get("name") for t in scene.get("tags") or [] if t.get("name")]
    if dates_info.pop("_is_recent", False): item_tags.append("Recently Added")
    
    # FIX 2: Safely check o_counter if it explicitly returns null
    if (scene.get("o_counter") or 0) >= 1: 
        item_tags.append("Onot0")
    
    item["Tags"] = item_tags
    item["Genres"] = item_tags[:10]
    
    item.update(dates_info)

    # Optional Overview Generator
    overview_parts = [scene.get("details", "")] if scene.get("details") else []
    if scene.get("studio") and scene["studio"].get("name"): overview_parts.append(f"Studio: {scene['studio']['name']}")
    item["Overview"] = "\n\n".join(overview_parts)
    
    return item