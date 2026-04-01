import logging
import httpx
from typing import Dict, Any, Optional
import config

logger = logging.getLogger(__name__)

SCENE_FIELDS = """
    id title code date details o_counter play_count rating100 created_at organized resume_time
    files { path duration video_codec audio_codec frame_rate bit_rate width height format size } 
    studio { id name image_path } 
    tags { name } 
    performers { name id image_path } 
    captions { language_code caption_type }
"""

def get_stash_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if getattr(config, "STASH_API_KEY", ""): headers["ApiKey"] = config.STASH_API_KEY
    return headers

async def test_stash_connection() -> bool:
    url = f"{config.get_stash_base()}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        try:
            response = await client.post(url, headers=get_stash_headers(), json={"query": "{ version { version } }"}, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            if "data" in data and "version" in data["data"]: return True
            return False
        except Exception as e: return False

async def call_graphql(query: str, variables: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    url = f"{config.get_stash_base()}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    payload = {"query": query}
    if variables: payload["variables"] = variables

    async with httpx.AsyncClient(verify=getattr(config, "STASH_VERIFY_TLS", False)) as client:
        for attempt in range(1, getattr(config, "STASH_RETRIES", 3) + 1):
            try:
                response = await client.post(url, headers=get_stash_headers(), json=payload, timeout=getattr(config, "STASH_TIMEOUT", 30))
                response.raise_for_status()
                result = response.json()
                if "errors" in result: return None
                return result.get("data")
            except Exception as e:
                if attempt == getattr(config, "STASH_RETRIES", 3): return None
                import asyncio
                await asyncio.sleep(1.0)

async def get_scene(scene_id: str) -> Optional[Dict[str, Any]]:
    data = await call_graphql(f"query FindScene($id: ID!) {{ findScene(id: $id) {{ {SCENE_FIELDS} }} }}", {"id": scene_id})
    return data["findScene"] if data and data.get("findScene") else None

# --- REFACTORED: Removed SYNC_LEVEL business logic entirely ---
async def fetch_scenes(filter_args: Dict[str, Any], page: int = 1, per_page: int = 50, scene_filter: Dict[str, Any] = None) -> Dict[str, Any]:
    sf = scene_filter or {}
    if "title" in filter_args: sf["title"] = filter_args.pop("title")
    
    query = f"query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {{ findScenes(filter: $filter, scene_filter: $scene_filter) {{ count scenes {{ {SCENE_FIELDS} }} }} }}"
    
    filter_args["page"] = page
    filter_args["per_page"] = per_page
    data = await call_graphql(query, {"filter": filter_args, "scene_filter": sf})
    return data.get("findScenes") if data else {"count": 0, "scenes": []}

async def get_stash_stats() -> dict:
    data = await call_graphql("query Stats { stats { scene_count performer_count studio_count tag_count group_count } }")
    return data["stats"] if data and "stats" in data else {}

async def get_all_studios():
    data = await call_graphql("query AllStudios { allStudios { id name image_path } }")
    return data.get("allStudios", []) if data else []

async def update_resume_time(scene_id: str, time_seconds: float):
    await call_graphql("mutation SceneSaveActivity($id: ID!, $resume_time: Float) { sceneSaveActivity(id: $id, resume_time: $resume_time) }", {"id": scene_id, "resume_time": time_seconds})

async def increment_play_count(scene_id: str):
    await call_graphql("mutation($id: ID!) { sceneIncrementPlayCount(id: $id) }", {"id": scene_id})

async def increment_o_counter(scene_id: str):
    await call_graphql("mutation SceneAddO($id: ID!, $times: [Timestamp!]) { sceneAddO(id: $id, times: $times) { count } }", {"id": scene_id})

async def update_rating(scene_id: str, rating100: int):
    await call_graphql("mutation SceneUpdate($input: SceneUpdateInput!) { sceneUpdate(input: $input) { id } }", {"input": {"id": scene_id, "rating100": rating100}})

async def destroy_scene(scene_id: str, delete_file: bool = False) -> bool:
    result = await call_graphql("mutation sceneDestroy($input: SceneDestroyInput!) { sceneDestroy(input: $input) }", {"input": {"id": scene_id, "delete_file": delete_file, "delete_generated": True}})
    return result is not None and result.get("sceneDestroy") is True

async def get_all_tags() -> list:
    data = await call_graphql("""query { findTags(filter: {per_page: -1, sort: "name", direction: ASC}) { tags { id name } } }""")
    return data.get("findTags", {}).get("tags", []) if data else []

async def get_saved_filters() -> list:
    data = await call_graphql("""query { findSavedFilters(mode: SCENES) { id name find_filter { q sort direction } object_filter } }""")
    if data and data.get("findSavedFilters"): return data["findSavedFilters"]
    data_legacy = await call_graphql("""query { findSavedFilters(mode: SCENES) { id name filter find_filter { q sort direction } } }""")
    return data_legacy.get("findSavedFilters", []) if data_legacy else []

async def get_performer(performer_id: str) -> dict:
    data = await call_graphql("""query FindPerformer($id: ID!) { findPerformer(id: $id) { id name image_path } }""", {"id": performer_id})
    return data.get("findPerformer", {}) if data else {}

async def get_scene_sprite(scene_id: str) -> str:
    data = await call_graphql("""query($id: ID!) { findScene(id: $id) { paths { sprite } } }""", {"id": scene_id})
    return data.get("findScene", {}).get("paths", {}).get("sprite") if data else None