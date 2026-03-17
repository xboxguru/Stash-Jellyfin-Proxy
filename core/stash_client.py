import logging
import requests
from typing import Dict, Any, Optional
import config

logger = logging.getLogger(__name__)

# The universal GraphQL fields we request for scenes. 
SCENE_FIELDS = """
    id title code date details o_counter created_at organized resume_time
    files { path duration video_codec audio_codec frame_rate bit_rate width height format } 
    studio { id name image_path } 
    tags { name } 
    performers { name id image_path } 
    captions { language_code caption_type }
"""

def get_stash_headers() -> Dict[str, str]:
    """Build the headers required to talk to Stash."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if config.STASH_API_KEY:
        headers["ApiKey"] = config.STASH_API_KEY
    return headers

def test_stash_connection() -> bool:
    """Check if Stash is online and reachable."""
    url = f"{config.STASH_URL.rstrip('/')}{config.STASH_GRAPHQL_PATH}"
    query = {"query": "{ version { version } }"}
    try:
        response = requests.post(
            url, 
            headers=get_stash_headers(), 
            json=query, 
            timeout=10, 
            verify=config.STASH_VERIFY_TLS
        )
        response.raise_for_status()
        data = response.json()
        if "data" in data and "version" in data["data"]:
            logger.info(f"Successfully connected to Stash (Version: {data['data']['version']['version']})")
            return True
        logger.error("Connected to Stash, but received unexpected GraphQL response.")
        return False
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to connect to Stash at {url}: {e}")
        return False

def call_graphql(query: str, variables: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Execute a GraphQL query against Stash with retry logic."""
    url = f"{config.STASH_URL.rstrip('/')}{config.STASH_GRAPHQL_PATH}"
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    for attempt in range(1, config.STASH_RETRIES + 1):
        try:
            response = requests.post(
                url, 
                headers=get_stash_headers(), 
                json=payload, 
                timeout=config.STASH_TIMEOUT, 
                verify=config.STASH_VERIFY_TLS
            )
            response.raise_for_status()
            result = response.json()
            if "errors" in result:
                logger.error(f"GraphQL Error: {result['errors']}")
                return None
            return result.get("data")
        except requests.exceptions.RequestException as e:
            logger.warning(f"Stash API request failed (Attempt {attempt}/{config.STASH_RETRIES}): {e}")
            if attempt == config.STASH_RETRIES:
                logger.error("Max retries reached. Stash is unreachable.")
                return None

def get_scene(scene_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single scene by ID. Does NOT enforce sync filters to allow access from override folders."""
    query = f"""
    query FindScene($id: ID!) {{
        findScene(id: $id) {{
            {SCENE_FIELDS}
        }}
    }}
    """
    data = call_graphql(query, {"id": scene_id})
    if data and data.get("findScene"):
        return data["findScene"]
        
    return None

def fetch_scenes(filter_args: Dict[str, Any], page: int = 1, per_page: int = 50, scene_filter: Dict[str, Any] = None, ignore_sync_level: bool = False) -> Dict[str, Any]:
    """Fetch scenes based on SYNC_LEVEL, with an override for specific library folders."""
    sf = scene_filter or {}
    
    if "title" in filter_args:
        sf["title"] = filter_args.pop("title")
        
    # Apply global SYNC_LEVEL filter ONLY if we aren't already filtering by a specific folder
    if not ignore_sync_level:
        sync_mode = getattr(config, "SYNC_LEVEL", "Everything")
        if sync_mode == "Organized":
            sf["organized"] = True 
        elif sync_mode == "Tagged":
            sf["tags"] = {"modifier": "NOT_NULL"}  # Stash filter for 'has any tag'

    query = f"""
    query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {{
        findScenes(filter: $filter, scene_filter: $scene_filter) {{
            count
            scenes {{
                {SCENE_FIELDS}
            }}
        }}
    }}
    """
    
    filter_args["page"] = page
    filter_args["per_page"] = per_page
    
    variables = {
        "filter": filter_args,
        "scene_filter": sf
    }
    
    data = call_graphql(query, variables)
    return data.get("findScenes") if data else {"count": 0, "scenes": []}

def get_stash_stats() -> dict:
    """Fetches total library counts from Stash."""
    query = """
    query Stats {
        stats {
            scene_count
            performer_count
            studio_count
            tag_count
            group_count
        }
    }
    """
    data = call_graphql(query)
    if data and "stats" in data:
        return data["stats"]
    return {}

def get_all_studios():
    """Fetches all studios from Stash to populate the 'Networks' list in ErsatzTV."""
    query = """
    query AllStudios {
      allStudios {
        id
        name
        image_path
      }
    }
    """
    data = call_graphql(query)
    return data.get("allStudios", []) if data else []