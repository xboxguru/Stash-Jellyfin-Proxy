import os
import logging
import httpx
from starlette.responses import Response
from starlette.requests import Request
import config
import re
from core.jellyfin_mapper import decode_id, encode_id

logger = logging.getLogger(__name__)

async def endpoint_item_image(request: Request):
    raw_encoded_id = request.path_params.get("item_id", "")
    decoded_id = decode_id(raw_encoded_id)
    
    # 1. CUSTOM LIBRARY & TAG GROUP LOGO INTERCEPT
    # This catches "root-scenes" and any "tag-ID" folder requests
    if "root-" in decoded_id or "tag-" in decoded_id:
        # Look for a custom logo.png in the main proxy directory
        logo_path = os.path.join(os.getcwd(), "logo.png")
        if os.path.exists(logo_path):
            with open(logo_path, "rb") as f:
                return Response(content=f.read(), media_type="image/png")
        else:
            # Fallback: Serve an elegant Stash SVG logo generated in code
            svg_data = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 400" width="400" height="400">
              <rect width="400" height="400" fill="#1B1C26"/>
              <text x="50%" y="45%" dominant-baseline="middle" text-anchor="middle" font-family="sans-serif" font-size="70" fill="#FFFFFF" font-weight="bold">STASH</text>
              <text x="50%" y="60%" dominant-baseline="middle" text-anchor="middle" font-family="sans-serif" font-size="30" fill="#00E5FF">SCENES</text>
            </svg>'''
            return Response(content=svg_data, media_type="image/svg+xml")

    # 2. Extract ONLY the digits for the Stash URL (e.g., '11')
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