import os
import datetime
import hashlib
from typing import Dict, Any
import config

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
    
    # If the user passed in 'scene-11' directly, return it
    if clean_id.startswith("scene") or clean_id.startswith("person") or clean_id.startswith("studio"):
        return encoded_id 
        
    try:
        # Convert hex back to bytes, then decode, then strip all null padding
        decoded_bytes = bytes.fromhex(clean_id)
        decoded_str = decoded_bytes.decode('utf-8').rstrip('\x00')
        
        # Verify it's one of our internal formats
        if "scene-" in decoded_str or "person-" in decoded_str or "studio-" in decoded_str or "tag-" in decoded_str:
            return decoded_str.strip()
    except Exception as e:
        print(f"DECODE ERROR on {encoded_id}: {e}")
        pass
        
    return encoded_id

# ADDED: Hyphen helper for strict Kotlin UserData parsing
def hyphens(h: str) -> str:
    if len(h) != 32: return h
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"

def format_jellyfin_item(scene: Dict[str, Any], parent_id: str = None) -> Dict[str, Any]:
    """
    Transforms a Stash Scene object into a Jellyfin Movie/Video object.
    """
    raw_id = str(scene.get("id"))
    item_id = encode_id("scene", raw_id)
    date = scene.get("date")
    cache_version = getattr(config, "CACHE_VERSION", 0)
    
    # Safely generate the 32-character parent ID
    final_parent_id = parent_id if parent_id else encode_id("root", "scenes")

    resume_time_seconds = scene.get("resume_time") or 0
    resume_ticks = int(resume_time_seconds * 10000000)
    
    files = scene.get("files", [])
    path = files[0].get("path") if files else ""
    duration_seconds = files[0].get("duration", 0) if files else 0
    runtime_ticks = int(duration_seconds * 10000000)
    
    if path:
        path = path.replace("\\", "/")

    width = files[0].get("width", 0) if files else 0
    height = files[0].get("height", 0) if files else 0
    v_codec = files[0].get("video_codec", "h264") if files else "h264"
    a_codec = files[0].get("audio_codec", "aac") if files else "aac"
    container = files[0].get("format", "mp4") if files else "mp4"
    bit_rate = files[0].get("bit_rate", 0) if files else 0

    title = scene.get("title") or scene.get("code")
    if not title and path:
        filename = os.path.basename(path)
        title = os.path.splitext(filename)[0] if filename else None
    if not title:
        title = f"Scene {raw_id}"
        
    studio_obj = scene.get("studio")
    studio_name = studio_obj.get("name") if studio_obj else None
    description = scene.get("details") or ""
    tags = scene.get("tags", [])
    performers = scene.get("performers", [])
    play_count = scene.get("o_counter", 0) or 0
    
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    # --- STRICT FINDROID MEDIA STREAMS ---
    # Create a base dictionary with all the mandatory boolean flags
    base_stream_flags = {
        "IsInterlaced": False,
        "IsDefault": True,
        "IsForced": False,
        "IsHearingImpaired": False,
        "IsExternal": False,
        "IsTextSubtitleStream": False,
        "SupportsExternalStream": False,
        "IsAVC": False
    }

    video_stream = base_stream_flags.copy()
    video_stream.update({
        "Codec": v_codec,
        "Type": "Video",
        "Width": width,
        "Height": height,
        "Index": 0,
        "BitRate": bit_rate,
        "IsAVC": v_codec.lower() in ["h264", "avc"]
    })

    audio_stream = base_stream_flags.copy()
    audio_stream.update({
        "Codec": a_codec,
        "Type": "Audio",
        "Index": 1,
        "Channels": 2
    })

    media_streams = [video_stream, audio_stream]

    # Build the core Jellyfin Item
    item = {
        "Name": title,
        "SortName": title,
        "Id": item_id,
        "ServerId": getattr(config, "SERVER_ID", "stash-proxy"),
        "Type": "Movie",
        "IsFolder": False,
        "MediaType": "Video",
        "ParentId": final_parent_id,
        "DateLastSaved": now_iso, 
        
        "ChannelId": None,
        "Container": container,
        "ImageBlurHashes": {},
        
        "HasPrimaryImage": True,
        "ImageTags": {"Primary": f"{raw_id}-v{cache_version}"}, 
        "PrimaryImageAspectRatio": 1.777,
        "VideoType": "VideoFile",
        "Protocol": "File",
        "BackdropImageTags": [],
        
        "RunTimeTicks": runtime_ticks,
        "OfficialRating": "XXX",
        "CommunityRating": play_count,
        "Width": width,
        "Height": height,

        "Etag": f"etag-{raw_id}-v{cache_version}",
        "Taglines": [],
        "ProviderIds": {},
        "Chapters": [],
        "Overview": "",
        "People": [],
        "Studios": [],
        "MediaStreams": media_streams,
        "Path": path if path else "",

        "_StashVideoCodec": v_codec,
        "_StashAudioCodec": a_codec,
        "_StashContainer": container,
        "_StashBitRate": bit_rate,

        "UserData": {
            "PlaybackPositionTicks": resume_ticks,
            "PlayCount": play_count,
            "IsFavorite": False,
            "Played": play_count > 0,
            "Key": hyphens(item_id),
            "ItemId": item_id
        }
    }

    item_tags = [t.get("name") for t in tags if t.get("name")]
    created_at = scene.get("created_at")
    recent_days_limit = getattr(config, "RECENT_DAYS", 14)

    if created_at:
        base_time = created_at.replace("Z", "").replace(" ", "T")[:19]
        formatted_created = f"{base_time}.0000000Z"
        if recent_days_limit > 0:
            try:
                dt = datetime.datetime.strptime(base_time, "%Y-%m-%dT%H:%M:%S")
                if (datetime.datetime.utcnow() - dt).days <= recent_days_limit:
                    item_tags.append("Recently Added")
            except Exception:
                pass 
    else:
        formatted_created = now_iso

    item["DateCreated"] = formatted_created

    if date and len(date) >= 4:
        try:
            item["ProductionYear"] = int(date[:4])
            item["PremiereDate"] = f"{date}T00:00:00.0000000Z"
        except:
            item["PremiereDate"] = formatted_created
            item["ProductionYear"] = int(formatted_created[:4])
    else:
        item["PremiereDate"] = formatted_created
        item["ProductionYear"] = int(formatted_created[:4])

    overview_parts = []
    if description:
        overview_parts.append(description)
    if studio_name:
        overview_parts.append(f"Studio: {studio_name}")
    if overview_parts:
        item["Overview"] = "\n\n".join(overview_parts)

    if play_count >= 1:
        item_tags.append("Onot0")
    
    item["Tags"] = item_tags
    item["Genres"] = item_tags[:10]

    if performers:
        people_list = []
        for p in performers:
            p_name = p.get("name")
            p_id = p.get("id")
            if p_name and p_id:
                p_tag = f"p-{p_id}-v{cache_version}"
                has_image = bool(p.get("image_path"))
            
                person = {
                    "Name": p_name,
                    "Type": "Actor",
                    "Role": "",
                    "Id": encode_id("person", str(p_id)),
                    "PrimaryImageTag": p_tag if has_image else None
                }
                if has_image:
                    person["ImageTags"] = {"Primary": p_tag}
                people_list.append(person)
        item["People"] = people_list

    if studio_obj and studio_name:
        studio_id = studio_obj.get("id")
        has_studio_image = bool(studio_obj.get("image_path"))
        s_tag = f"s-{studio_id}-v{cache_version}"
        
        studio_item = {
            "Name": studio_name,
            "Id": encode_id("studio", str(studio_id))
        }
        
        if has_studio_image:
            studio_item["PrimaryImageTag"] = s_tag
            studio_item["ImageTags"] = {"Primary": s_tag}
            
        item["Studios"] = [studio_item]

    if path:
        item["Path"] = path
        item["LocationType"] = "FileSystem"
        stream_url = f"/Videos/{item_id}/stream"
        
        item["MediaSources"] = [
            {
                "Id": item_id,
                "Path": path,
                "DirectStreamUrl": stream_url,
                "Protocol": "File",
                "Type": "Default",
                "Container": container,
                "RunTimeTicks": runtime_ticks, 
                "IsRemote": False,
                "SupportsDirectPlay": True,
                "SupportsDirectStream": True,
                "SupportsTranscoding": True,
                "VideoType": "VideoFile",
                "MediaStreams": media_streams,
                "MediaAttachments": [],
                "Formats": [],
                "RequiredHttpHeaders": {},
                "Name": title,
                "Size": 0,
                
                # --- ADDED: STRICT FINDROID MEDIASOURCEINFO FIELDS ---
                "ReadAtNativeFramerate": False,
                "IgnoreDts": False,
                "IgnoreIndex": False,
                "GenPtsInput": False,
                "IsInfiniteStream": False,
                "RequiresOpening": False,
                "RequiresClosing": False,
                "RequiresLooping": False,
                "SupportsProbing": True,
                "TranscodingSubProtocol": "http",
                "HasSegments": False,
                "UseMostCompatibleTranscodingProfile": False,
                "DefaultAudioStreamIndex": 1
            }
        ]

    return item