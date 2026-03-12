import logging
import httpx
from starlette.responses import Response
from starlette.requests import Request
import config

logger = logging.getLogger(__name__)

async def endpoint_item_image(request: Request):
    item_id = request.path_params.get("item_id", "")
    
    stash_base = getattr(config, "STASH_URL", "http://localhost:9999").rstrip('/')
    params = {}
    if getattr(config, "STASH_API_KEY", ""):
        params["apikey"] = config.STASH_API_KEY

    if item_id.startswith("person-"):
        raw_id = item_id.replace("person-", "")
        url = f"{stash_base}/performer/{raw_id}/image"
    elif item_id.startswith("studio-"):
        raw_id = item_id.replace("studio-", "")
        url = f"{stash_base}/studio/{raw_id}/image"
    else:
        raw_id = item_id.replace("scene-", "")
        url = f"{stash_base}/scene/{raw_id}/screenshot"
        
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            resp = await client.get(url, params=params, timeout=10.0)
            if resp.status_code == 200:
                return Response(content=resp.content, media_type=resp.headers.get("Content-Type", "image/jpeg"))
            else:
                return Response(status_code=404)
        except Exception as e:
            logger.error(f"IMAGE ERROR: Failed to proxy image from Stash: {e}")
            return Response(status_code=500)