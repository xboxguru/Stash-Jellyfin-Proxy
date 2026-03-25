import logging
import httpx
from typing import Dict, Any, Optional
import config

logger = logging.getLogger(__name__)

# The universal GraphQL fields we request for scenes. 
SCENE_FIELDS = """
    id title code date details o_counter play_count rating100 created_at organized resume_time
    files { path duration video_codec audio_codec frame_rate bit_rate width height format size } 
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
    if getattr(config, "STASH_API_KEY", ""):
        headers["ApiKey"] = config.STASH_API_KEY
    return headers

async def test_stash_connection() -> bool:
    """Check if Stash is online and reachable using Async httpx."""
    url = f"{config.get_stash_base()}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    query = {"query": "{ version { version } }"}
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            response = await client.post(url, headers=get_stash_headers(), json=query, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            if "data" in data and "version" in data["data"]:
                logger.info(f"Successfully connected to Stash (Version: {data['data']['version']['version']})")
                return True
            logger.error("Connected to Stash, but received unexpected GraphQL response.")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to Stash at {url}: {e}")
            return False

async def call_graphql(query: str, variables: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Execute an Async GraphQL query against Stash with retry logic."""
    url = f"{config.get_stash_base()}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        for attempt in range(1, getattr(config, "STASH_RETRIES", 3) + 1):
            try:
                response = await client.post(
                    url, 
                    headers=get_stash_headers(), 
                    json=payload, 
                    timeout=getattr(config, "STASH_TIMEOUT", 30)
                )
                response.raise_for_status()
                result = response.json()
                if "errors" in result:
                    logger.error(f"GraphQL Error: {result['errors']}")
                    return None
                return result.get("data")
            except Exception as e:
                logger.warning(f"Stash API request failed (Attempt {attempt}/{getattr(config, 'STASH_RETRIES', 3)}): {e}")
                if attempt == getattr(config, "STASH_RETRIES", 3):
                    logger.error("Max retries reached. Stash is unreachable.")
                    return None
                import asyncio
                await asyncio.sleep(1.0)

async def get_scene(scene_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single scene by ID asynchronously."""
    query = f"""
    query FindScene($id: ID!) {{
        findScene(id: $id) {{
            {SCENE_FIELDS}
        }}
    }}
    """
    data = await call_graphql(query, {"id": scene_id})
    if data and data.get("findScene"):
        return data["findScene"]
        
    return None

async def fetch_scenes(filter_args: Dict[str, Any], page: int = 1, per_page: int = 50, scene_filter: Dict[str, Any] = None, ignore_sync_level: bool = False) -> Dict[str, Any]:
    """Fetch scenes asynchronously based on SYNC_LEVEL."""
    sf = scene_filter or {}
    
    if "title" in filter_args:
        sf["title"] = filter_args.pop("title")
        
    if not ignore_sync_level:
        sync_mode = getattr(config, "SYNC_LEVEL", "Everything")
        if sync_mode == "Organized":
            sf["organized"] = True 
        elif sync_mode == "Tagged":
            sf["tags"] = {"modifier": "NOT_NULL"}

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
    
    data = await call_graphql(query, variables)
    return data.get("findScenes") if data else {"count": 0, "scenes": []}

async def get_stash_stats() -> dict:
    """Fetches total library counts from Stash asynchronously."""
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
    data = await call_graphql(query)
    if data and "stats" in data:
        return data["stats"]
    return {}

async def get_all_studios():
    """Fetches all studios from Stash asynchronously."""
    query = """
    query AllStudios {
      allStudios {
        id
        name
        image_path
      }
    }
    """
    data = await call_graphql(query)
    return data.get("allStudios", []) if data else []

# =====================================================================
# MUTATIONS (User Data & Deletion)
# =====================================================================

async def update_resume_time(scene_id: str, time_seconds: float):
    """Saves the user's playback progress for a scene."""
    query = "mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }"
    await call_graphql(query, {"id": scene_id, "resume_time": time_seconds})
    logger.info(f"✅ Two-Way Sync: Saved resume time ({time_seconds}s) for Scene {scene_id}")

async def increment_play_count(scene_id: str):
    """Increments the official Stash play count for a scene."""
    query = "mutation($id: ID!) { sceneIncrementPlayCount(id: $id) }"
    await call_graphql(query, {"id": scene_id})
    logger.info(f"✅ Two-Way Sync: Logged official Play event for Scene {scene_id}")

async def increment_o_counter(scene_id: str):
    """Increments the Stash O-Counter for a scene."""
    query = "mutation SceneAddO($id: ID!, $times: [Timestamp!]) { sceneAddO(id: $id, times: $times) { count } }"
    await call_graphql(query, {"id": scene_id})
    logger.info(f"✅ Two-Way Sync: Added 'O' event for Scene {scene_id}")

async def update_rating(scene_id: str, rating100: int):
    """Updates the 1-100 star rating for a scene."""
    query = "mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }"
    await call_graphql(query, {"input": {"id": scene_id, "rating100": rating100}})
    logger.info(f"✅ Two-Way Sync: Updated Rating for Scene {scene_id} to {rating100}")

async def destroy_scene(scene_id: str, delete_file: bool = False) -> bool:
    """Removes a scene from the database, and optionally deletes the physical file."""
    query = "mutation sceneDestroy($input: SceneDestroyInput!) { sceneDestroy(input: $input) }"
    variables = {"input": {"id": scene_id, "delete_file": delete_file, "delete_generated": True}}
    result = await call_graphql(query, variables)
    return result is not None and result.get("sceneDestroy") is True

# =====================================================================
# SPECIFIC QUERIES (Metadata & Assets)
# =====================================================================

async def get_all_tags() -> list:
    """Fetches all Stash tags."""
    query = """query { findTags(filter: {per_page: -1, sort: "name", direction: ASC}) { tags { id name } } }"""
    data = await call_graphql(query)
    return data.get("findTags", {}).get("tags", []) if data else []

async def get_saved_filters() -> list:
    """Fetches all saved filters, attempting modern schema first, then legacy."""
    query_modern = """query { findSavedFilters(mode: SCENES) { id name find_filter { q sort direction } object_filter } }"""
    data = await call_graphql(query_modern)
    if data and data.get("findSavedFilters"): return data["findSavedFilters"]
    
    query_legacy = """query { findSavedFilters(mode: SCENES) { id name filter find_filter { q sort direction } } }"""
    data_legacy = await call_graphql(query_legacy)
    return data_legacy.get("findSavedFilters", []) if data_legacy else []

async def get_performer(performer_id: str) -> dict:
    """Fetches a specific performer by ID."""
    query = """query FindPerformer($id: ID!) { findPerformer(id: $id) { id name image_path } }"""
    data = await call_graphql(query, {"id": performer_id})
    return data.get("findPerformer", {}) if data else {}

async def get_scene_sprite(scene_id: str) -> str:
    """Fetches the specific Sprite/Trickplay URL for a scene."""
    query = """query($id: ID!) { findScene(id: $id) { paths { sprite } } }"""
    data = await call_graphql(query, {"id": scene_id})
    return data.get("findScene", {}).get("paths", {}).get("sprite") if data else None