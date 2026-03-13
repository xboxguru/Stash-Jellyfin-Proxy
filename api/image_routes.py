import logging
import httpx
from starlette.responses import Response
from starlette.requests import Request
import config
import re
from core.jellyfin_mapper import decode_id

logger = logging.getLogger(__name__)

async def endpoint_item_image(request: Request):
    raw_encoded_id = request.path_params.get("item_id", "")
    
    # 1. Decode the hex (e.g., 'scene-11\x00\x00')
    decoded_id = decode_id(raw_encoded_id)
    
    # 2. Extract ONLY the digits for the URL (e.g., '11')
    number_match = re.search(r'\d+', decoded_id)
    raw_id = number_match.group() if number_match else ""

    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
    
    params = {}
    if getattr(config, "STASH_API_KEY", ""):
        params["apikey"] = config.STASH_API_KEY

    # 3. Use the DECODED_ID for logic, and the RAW_ID for the URL
    if "person-" in decoded_id:
        url = f"{stash_base}/performer/{raw_id}/image"
    elif "studio-" in decoded_id:
        url = f"{stash_base}/studio/{raw_id}/image"
    else:
        # Default to scene screenshot
        url = f"{stash_base}/scene/{raw_id}/screenshot"
        
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            if resp.status_code == 200:
                return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
            else:
                logger.warning(f"Stash returned {resp.status_code} for image: {url}")
                return Response(status_code=404)
        except Exception as e:
            logger.error(f"IMAGE ERROR: Failed to proxy image from Stash: {e}")
            return Response(status_code=500)