import os
import logging
import httpx
from starlette.responses import FileResponse, Response
from starlette.requests import Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse
import config
from core.jellyfin_mapper import decode_id

logger = logging.getLogger(__name__)

async def endpoint_item_image(request: Request):
    """
    Handles all image requests (Primary, Backdrop, Logo, Thumb).
    Heavily instrumented for debugging Fladder image issues.
    """
    # 1. Extract raw parameters
    raw_item_id = request.path_params.get("item_id", "")
    raw_image_type = request.path_params.get("image_type", "Primary")
    
    # 2. Decode the ID
    item_id = decode_id(raw_item_id)
    image_type = raw_image_type.lower()
    
    logger.info(f"📸 IMAGE REQUEST DETECTED | Raw ID: '{raw_item_id}' | Decoded ID: '{item_id}' | Type: '{image_type}'")

    # 3. Handle Scenes (Movies) First!
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        stash_base = config.get_stash_base()
        apikey = getattr(config, "STASH_API_KEY", "")
        stash_img_url = f"{stash_base}/scene/{raw_id}/screenshot"
        if apikey: stash_img_url += f"?apikey={apikey}"
        logger.info(f"🖼️ ROUTING TO SCENE: {item_id} ({image_type}) -> {stash_img_url}")
        return await _proxy_image(stash_img_url)

    # 4. Handle Performers (Actors) - NOW PROXIED INSTEAD OF REDIRECTED
    if item_id.startswith("person-") or item_id.startswith("performer-"):
        raw_id = item_id.replace("person-", "").replace("performer-", "")
        stash_base = config.get_stash_base()
        apikey = getattr(config, "STASH_API_KEY", "")
        stash_img_url = f"{stash_base}/performer/{raw_id}/image"
        if apikey: stash_img_url += f"?apikey={apikey}"
        logger.info(f"🖼️ ROUTING TO PERFORMER: {item_id} -> {stash_img_url}")
        return await _proxy_image(stash_img_url)

    # 5. Handle Studios
    if item_id.startswith("studio-"):
        raw_id = item_id.replace("studio-", "")
        stash_base = config.get_stash_base()
        apikey = getattr(config, "STASH_API_KEY", "")
        stash_img_url = f"{stash_base}/studio/{raw_id}/image"
        if apikey: stash_img_url += f"?apikey={apikey}"
        logger.info(f"🖼️ ROUTING TO STUDIO: {item_id} -> {stash_img_url}")
        return await _proxy_image(stash_img_url)

    # 6. Handle Root Libraries & Tags (Fallback to Logo)
    if item_id.startswith("root-") or item_id.startswith("tag-") or item_id.startswith("filter-") or item_id == raw_item_id:
        logo_path = os.path.join(os.getcwd(), "logo.png")
        if os.path.exists(logo_path):
            return FileResponse(logo_path, media_type="image/png")
        else:
            return Response(status_code=404)

    logger.warning(f"⚠️ UNHANDLED IMAGE REQUEST: {item_id} | Returning 404")
    return Response(status_code=404)

from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

async def _proxy_image(url: str):
    """Helper function to stream the image from Stash without hoarding RAM."""
    client = httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False))
    try:
        req = client.build_request("GET", url)
        r = await client.send(req, stream=True)
        
        if r.status_code == 200:
            content_type = r.headers.get("content-type", "image/jpeg")
            
            async def stream_generator():
                async for chunk in r.aiter_bytes(chunk_size=8192):
                    yield chunk
                    
            async def cleanup():
                await r.aclose()
                await client.aclose()

            return StreamingResponse(stream_generator(), media_type=content_type, background=BackgroundTask(cleanup))
        else:
            logger.error(f"❌ STASH RETURNED HTTP {r.status_code} for URL: {url}")
            await r.aclose()
            await client.aclose()
            
    except Exception as e:
        logger.error(f"❌ FAILED TO FETCH IMAGE FROM STASH: {e}")
        await client.aclose()
        
    return Response(status_code=404)

async def endpoint_trickplay_image(request: Request):
    """Intercepts trickplay requests and securely fetches the sprite via GraphQL."""
    item_id = request.path_params.get("item_id", "")
    file_name = request.path_params.get("file_name", "").lower()
    decoded_id = decode_id(item_id)
    
    # 1. THE PLAYLIST SPOOFER: Satisfy Fladder's HLS Trickplay requirement
    if file_name == "tiles.m3u8":
        logger.info(f"📜 TRICKPLAY REQUEST: Serving dynamic HLS playlist to Fladder")
        m3u8_content = (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:36000\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXT-X-PLAYLIST-TYPE:VOD\n"
            "#EXTINF:36000.0,\n"
            "0.jpg\n"
            "#EXT-X-ENDLIST\n"
        )
        return Response(content=m3u8_content, media_type="application/vnd.apple.mpegurl")

    # 2. Prevent unsupported formats (like Roku .bif) from hanging the client
    if not file_name.endswith(".jpg") and not file_name.endswith(".jpeg"):
        logger.warning(f"⚠️ Client requested unsupported trickplay format: {file_name} | Returning 404")
        return Response(status_code=404)
        
    if decoded_id.startswith("scene-"):
        raw_id = decoded_id.replace("scene-", "")
        stash_base = config.get_stash_base()
        
        # 3. Ask GraphQL for the exact, native Sprite URL
        url = f"{stash_base}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if getattr(config, "STASH_API_KEY", ""):
            headers["ApiKey"] = config.STASH_API_KEY
            
        query = """
        query($id: ID!) {
            findScene(id: $id) {
                paths {
                    sprite
                }
            }
        }
        """
        
        stash_sprite_url = None
        async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
            try:
                resp = await client.post(url, headers=headers, json={"query": query, "variables": {"id": raw_id}}, timeout=5.0)
                data = resp.json()
                stash_sprite_url = data.get("data", {}).get("findScene", {}).get("paths", {}).get("sprite")
            except Exception as e:
                logger.error(f"⚠️ Failed to query GraphQL for sprite URL: {e}")

        # 4. Fetch the image from the correct URL
        if stash_sprite_url:
            logger.info(f"🎞️ TRICKPLAY REQUEST: Resolved Native Sprite URL -> {stash_sprite_url}")
            
            if stash_sprite_url.startswith("/"):
                stash_sprite_url = f"{stash_base}{stash_sprite_url}"
                
            apikey = getattr(config, "STASH_API_KEY", "")
            if apikey and "apikey=" not in stash_sprite_url:
                stash_sprite_url += f"&apikey={apikey}" if "?" in stash_sprite_url else f"?apikey={apikey}"

            return await _proxy_image(stash_sprite_url)

        # --- THE SHOCK ABSORBER ---
        logger.info(f"🛡️ SPRITE MISSING FOR {raw_id}: Returning 1x1 Black JPEG Fallback")
        blank_jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\x03\x02\x02\x02\x02\x02\x03\x02\x02\x02\x03\x03\x03\x03\x04\x06\x04\x04\x04\x04\x04\x08\x06\x06\x05\x06\t\x08\n\n\t\x08\t\t\n\x0c\x0f\x0c\n\x0b\x0e\x0b\t\t\r\x11\r\x0e\x0f\x10\x10\x11\x10\n\x0c\x12\x13\x12\x10\x13\x0f\x10\x10\x10\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\xd2\x8a\x28\xa0\x0f\xff\xd9'
        return Response(content=blank_jpeg, media_type="image/jpeg")

    return Response(status_code=404)