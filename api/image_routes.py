import os
import logging
import httpx
from starlette.responses import FileResponse, Response, StreamingResponse
from starlette.requests import Request
from starlette.background import BackgroundTask
import config
from core.jellyfin_mapper import decode_id

logger = logging.getLogger(__name__)

# --- THE FIX: Global Connection Pool for Images ---
image_client = httpx.AsyncClient(
    verify=getattr(config, "STASH_VERIFY_TLS", False), 
    timeout=10.0, 
    limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)
)

async def endpoint_item_image(request: Request):
    raw_item_id = request.path_params.get("item_id", "")
    item_id = decode_id(raw_item_id)
    image_type = request.path_params.get("image_type", "Primary").lower()
    
    logger.debug(f"📸 IMAGE REQUEST | Decoded ID: '{item_id}' | Type: '{image_type}'")

    type_map = {
        "scene-": ("scene", "screenshot"),
        "person-": ("performer", "image"),
        "performer-": ("performer", "image"),
        "studio-": ("studio", "image")
    }

    for prefix, (stash_route, stash_suffix) in type_map.items():
        if item_id.startswith(prefix):
            raw_id = item_id.replace(prefix, "")
            stash_base = config.get_stash_base()
            apikey = getattr(config, "STASH_API_KEY", "")
            
            stash_img_url = f"{stash_base}/{stash_route}/{raw_id}/{stash_suffix}"
            if apikey: stash_img_url += f"?apikey={apikey}"
                
            return await _proxy_image(stash_img_url)

    if any(item_id.startswith(p) for p in ["root-", "tag-", "filter-"]) or item_id == raw_item_id:
        logo_path = os.path.join(os.getcwd(), "logo.png")
        if os.path.exists(logo_path):
            return FileResponse(logo_path, media_type="image/png")

    return Response(status_code=404)

async def _proxy_image(url: str):
    """Streams the image using the shared, persistent HTTP client pool."""
    try:
        req = image_client.build_request("GET", url)
        r = await image_client.send(req, stream=True)
        
        if r.status_code == 200:
            content_type = r.headers.get("content-type", "image/jpeg")
            
            async def stream_generator():
                async for chunk in r.aiter_bytes(chunk_size=8192):
                    yield chunk
                    
            async def cleanup():
                await r.aclose()

            return StreamingResponse(stream_generator(), media_type=content_type, background=BackgroundTask(cleanup))
        else:
            await r.aclose()
            
    except Exception as e:
        logger.error(f"❌ FAILED TO FETCH IMAGE FROM STASH: {e}")
        
    return Response(status_code=404)

async def endpoint_trickplay_image(request: Request):
    item_id = request.path_params.get("item_id", "")
    file_name = request.path_params.get("file_name", "").lower()
    decoded_id = decode_id(item_id)
    
    if file_name == "tiles.m3u8":
        m3u8_content = (
            "#EXTM3U\n#EXT-X-TARGETDURATION:36000\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:36000.0,\n0.jpg\n#EXT-X-ENDLIST\n"
        )
        return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")

    if not file_name.endswith(".jpg") and not file_name.endswith(".jpeg"):
        return Response(status_code=404)
        
    if decoded_id.startswith("scene-"):
        raw_id = decoded_id.replace("scene-", "")
        stash_base = config.get_stash_base()
        
        url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if getattr(config, "STASH_API_KEY", ""): headers["ApiKey"] = config.STASH_API_KEY
            
        query = """query($id: ID!) { findScene(id: $id) { paths { sprite } } }"""
        
        stash_sprite_url = None
        try:
            resp = await image_client.post(url, headers=headers, json={"query": query, "variables": {"id": raw_id}})
            data = resp.json()
            stash_sprite_url = data.get("data", {}).get("findScene", {}).get("paths", {}).get("sprite")
        except Exception as e:
            logger.error(f"⚠️ Failed to query GraphQL for sprite URL: {e}")

        if stash_sprite_url:
            if stash_sprite_url.startswith("/"):
                stash_sprite_url = f"{stash_base}{stash_sprite_url}"
                
            apikey = getattr(config, "STASH_API_KEY", "")
            if apikey and "apikey=" not in stash_sprite_url:
                stash_sprite_url += f"&apikey={apikey}" if "?" in stash_sprite_url else f"?apikey={apikey}"

            return await _proxy_image(stash_sprite_url)

        blank_jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\x03\x02\x02\x02\x02\x02\x03\x02\x02\x02\x03\x03\x03\x03\x04\x06\x04\x04\x04\x04\x04\x08\x06\x06\x05\x06\t\x08\n\n\t\x08\t\t\n\x0c\x0f\x0c\n\x0b\x0e\x0b\t\t\r\x11\r\x0e\x0f\x10\x10\x11\x10\n\x0c\x12\x13\x12\x10\x13\x0f\x10\x10\x10\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\xd2\x8a\x28\xa0\x0f\xff\xd9'
        return Response(content=blank_jpeg, media_type="image/jpeg")

    return Response(status_code=404)