import os
import logging
import httpx
from starlette.responses import FileResponse, Response
from starlette.requests import Request
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

    # 3. Handle Root Libraries & Tags (Return custom Stash logo)
    if item_id.startswith("root-") or item_id.startswith("tag-") or item_id == raw_item_id:
        # If it didn't decode into a scene/person (meaning it's likely a user avatar or a root folder)
        logo_path = os.path.join(os.getcwd(), "logo.png")
        logger.info(f"🖼️ ROUTING TO LOGO: {item_id} -> Looking for file at: {logo_path}")
        
        if os.path.exists(logo_path):
            return FileResponse(logo_path, media_type="image/png")
        else:
            logger.error(f"❌ LOGO NOT FOUND on disk at: {logo_path}")
            return Response(status_code=404)

    # 4. Handle Performers (Actors)
    if item_id.startswith("person-"):
        raw_id = item_id.replace("person-", "")
        stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
        apikey = getattr(config, "STASH_API_KEY", "")
        stash_img_url = f"{stash_base}/performer/{raw_id}/image"
        if apikey:
            stash_img_url += f"?apikey={apikey}"
            
        logger.info(f"🖼️ ROUTING TO PERFORMER: {item_id} -> {stash_img_url}")
        return await _proxy_image(stash_img_url)

    # 5. Handle Studios
    if item_id.startswith("studio-"):
        raw_id = item_id.replace("studio-", "")
        stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
        apikey = getattr(config, "STASH_API_KEY", "")
        stash_img_url = f"{stash_base}/studio/{raw_id}/image"
        if apikey:
            stash_img_url += f"?apikey={apikey}"
            
        logger.info(f"🖼️ ROUTING TO STUDIO: {item_id} -> {stash_img_url}")
        return await _proxy_image(stash_img_url)

    # 6. Handle Scenes (Movies)
    if item_id.startswith("scene-"):
        raw_id = item_id.replace("scene-", "")
        stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
        apikey = getattr(config, "STASH_API_KEY", "")
        
        # Default to screenshot (Primary / Backdrop / Thumb)
        stash_img_url = f"{stash_base}/scene/{raw_id}/screenshot"
        
        if apikey:
            stash_img_url += f"?apikey={apikey}"
            
        logger.info(f"🖼️ ROUTING TO SCENE: {item_id} ({image_type}) -> {stash_img_url}")
        return await _proxy_image(stash_img_url)

    logger.warning(f"⚠️ UNHANDLED IMAGE REQUEST: {item_id} | Returning 404")
    return Response(status_code=404)

async def _proxy_image(url: str):
    """Helper function to fetch the image from Stash and stream it to the client."""
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            resp = await client.get(url, timeout=10.0)
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "image/jpeg")
                return Response(content=resp.content, media_type=content_type)
            else:
                logger.error(f"❌ STASH RETURNED HTTP {resp.status_code} for URL: {url}")
        except Exception as e:
            logger.error(f"❌ FAILED TO FETCH IMAGE FROM STASH: {e}")
    return Response(status_code=404)