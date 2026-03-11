import os
import datetime
from typing import Dict, Any
import config

def format_jellyfin_item(scene: Dict[str, Any], parent_id: str = "root-scenes") -> Dict[str, Any]:
    """
    Transforms a Stash Scene object into a Jellyfin Movie/Video object.
    Fully optimized for ErsatzTV's strict C# metadata and scheduler requirements.
    """
    raw_id = str(scene.get("id"))
    item_id = f"scene-{raw_id}"
    date = scene.get("date")
    cache_version = getattr(config, "CACHE_VERSION", 0)
    
    # Extract file details
    files = scene.get("files", [])
    path = files[0].get("path") if files else ""
    duration_seconds = files[0].get("duration", 0) if files else 0
    
    # CRITICAL: Convert seconds to 100-nanosecond ticks for ErsatzTV Scheduler
    runtime_ticks = int(duration_seconds * 10000000)
    
    # Normalize Windows backslashes to UNIX forward slashes for Path Replacements
    if path:
        path = path.replace("\\", "/")

    # Extract resolution and dynamic codecs
    width = files[0].get("width", 0) if files else 0
    height = files[0].get("height", 0) if files else 0
    v_codec = files[0].get("video_codec", "h264") if files else "h264"
    a_codec = files[0].get("audio_codec", "aac") if files else "aac"
    container = files[0].get("format", "mp4") if files else "mp4"
    bit_rate = files[0].get("bit_rate", 0) if files else 0

    # Fallback title generation
    title = scene.get("title") or scene.get("code")
    if not title and path:
        filename = os.path.basename(path)
        title = os.path.splitext(filename)[0] if filename else None
    if not title:
        title = f"Scene {raw_id}"
        
    # Studio & Network mapping
    studio_obj = scene.get("studio")
    studio_name = studio_obj.get("name") if studio_obj else None
    description = scene.get("details") or ""
    tags = scene.get("tags", [])
    performers = scene.get("performers", [])
    play_count = scene.get("o_counter", 0) or 0
    
    # For auto-refresh: Always report the current time as the "Last Saved" time
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.0000000Z")

    # Build Media Streams explicitly for Tunarr Schema
    media_streams = [
        {
            "Codec": v_codec,
            "Type": "Video",
            "IsInterlaced": False,
            "IsDefault": True,
            "Width": width,
            "Height": height,
            "Index": 0,
            "BitRate": bit_rate
        },
        {
            "Codec": a_codec,
            "Type": "Audio",
            "IsDefault": True,
            "Index": 1,
            "Channels": 2
        }
    ]

    # Build the core Jellyfin Item
    item = {
        "Name": title,
        "SortName": title,
        "Id": item_id,
        "ServerId": getattr(config, "SERVER_ID", "stash-proxy"),
        "Type": "Movie",
        "IsFolder": False,
        "MediaType": "Video",
        "ParentId": parent_id,
        "DateLastSaved": now_iso, 
        
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

        # --- TUNARR STRICT SCHEMA FIXES ---
        "Etag": f"etag-{raw_id}-v{cache_version}",
        "Taglines": [],
        "ProviderIds": {},
        "Chapters": [],
        "Overview": "",
        "People": [],
        "Studios": [],
        "MediaStreams": media_streams,
        "Path": path if path else "",
        # ----------------------------------

        "_StashVideoCodec": v_codec,
        "_StashAudioCodec": a_codec,
        "_StashContainer": container,
        "_StashBitRate": bit_rate,

        "UserData": {
            "PlaybackPositionTicks": 0,
            "PlayCount": play_count,
            "IsFavorite": False,
            "Played": play_count > 0,
            "Key": item_id
        }
    }

    # 1. Grab existing Stash tags and initialize the item tags array ONCE
    item_tags = [t.get("name") for t in tags if t.get("name")]

    # 2. Add Date Info & Dynamic "Recently Added" Tag
    created_at = scene.get("created_at")
    recent_days_limit = getattr(config, "RECENT_DAYS", 14)

    if created_at:
        # Force "YYYY-MM-DDTHH:MM:SS"
        base_time = created_at.replace("Z", "").replace(" ", "T")[:19]
        formatted_created = f"{base_time}.0000000Z"
        
        # Inject dynamic tag based on UI config
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

    # Tunarr Schema: Always provide PremiereDate and ProductionYear
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

    # Build Overview (Description + Studio)
    overview_parts = []
    if description:
        overview_parts.append(description)
    if studio_name:
        overview_parts.append(f"Studio: {studio_name}")
    if overview_parts:
        item["Overview"] = "\n\n".join(overview_parts)

    # Process additional status tags
    if play_count >= 1:
        item_tags.append("Onot0")
    
    # Assign the final accumulated tags array to the item
    item["Tags"] = item_tags
    item["Genres"] = item_tags[:10]

    # Process Performers as Actors
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
                    "Id": f"person-{p_id}",
                    "PrimaryImageTag": p_tag if has_image else None
                }
                if has_image:
                    person["ImageTags"] = {"Primary": p_tag}
                people_list.append(person)
        item["People"] = people_list

    # Process Studio as Network/Studio for ErsatzTV Watermarks
    if studio_obj and studio_name:
        studio_id = studio_obj.get("id")
        has_studio_image = bool(studio_obj.get("image_path"))
        
        s_tag = f"s-{studio_id}-v{cache_version}"
        
        studio_item = {
            "Name": studio_name,
            "Id": f"studio-{studio_id}"
        }
        
        if has_studio_image:
            studio_item["PrimaryImageTag"] = s_tag
            studio_item["ImageTags"] = {"Primary": s_tag}
            
        item["Studios"] = [studio_item]

    # MEDIA SOURCES: Fully dynamic metadata with Duration for Scheduler
    if path:
        item["Path"] = path
        item["LocationType"] = "FileSystem"
        
        item["MediaSources"] = [
            {
                "Id": item_id,
                "Path": path,
                "Protocol": "File",
                "Type": "Default",
                "Container": container,
                "RunTimeTicks": runtime_ticks, 
                "IsRemote": False,
                "SupportsDirectPlay": True,
                "SupportsDirectStream": True,
                "SupportsTranscoding": True,
                "VideoType": "VideoFile",
                "MediaStreams": media_streams
            }
        ]

    return item