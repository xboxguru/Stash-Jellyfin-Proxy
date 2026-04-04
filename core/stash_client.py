import logging
import asyncio
import time
import httpx
from typing import Dict, Any, Optional
import config

logger = logging.getLogger(__name__)

# Lightweight fields for fast library browsing (Grid View)
BASE_SCENE_FIELDS = """
    id title code date details o_counter play_count rating100 created_at organized resume_time
    files { path duration video_codec audio_codec frame_rate bit_rate width height format size basename } 
    studio { id name image_path } 
    tags { name } 
    performers { name id image_path } 
    captions { language_code caption_type }
"""

# Heavy fields including Markers for individual scene details and playback
DETAILED_SCENE_FIELDS = BASE_SCENE_FIELDS + """
    scene_markers { id seconds title primary_tag { name } }
"""

class _StashConnectionManager:
    """Manages a persistent HTTP connection pool for Stash GraphQL queries."""
    
    def __init__(self):
        self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                verify=getattr(config, "STASH_VERIFY_TLS", False),
                timeout=getattr(config, "STASH_TIMEOUT", 30),
                limits=httpx.Limits(max_keepalive_connections=50, max_connections=100)
            )
        return self._client

    def get_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if getattr(config, "STASH_API_KEY", ""): 
            headers["ApiKey"] = config.STASH_API_KEY
        return headers

    async def execute(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = f"{config.get_stash_base()}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
        payload = {"query": query}
        if variables: 
            payload["variables"] = variables

        query_preview = query.replace('\n', ' ')[:80]
        
        for attempt in range(1, getattr(config, "STASH_RETRIES", 3) + 1):
            start_time = time.time()
            try:
                response = await self.client.post(url, headers=self.get_headers(), json=payload)
                elapsed = time.time() - start_time
                
                # Detailed Error Logging for Stash 500/504s
                if response.status_code != 200:
                    logger.warning(f"GraphQL HTTP {response.status_code} on attempt {attempt}. Response: {response.text[:200]}")
                    response.raise_for_status()

                result = response.json()
                
                if "errors" in result: 
                    logger.error(f"GraphQL Data Error [{elapsed:.2f}s]: {result['errors']}")
                    return None
                    
                logger.debug(f"GraphQL Request successful in {elapsed:.2f}s | Query: {query_preview}...")
                return result.get("data")
                
            except Exception as e:
                elapsed = time.time() - start_time
                logger.warning(f"GraphQL request failed in {elapsed:.2f}s (Attempt {attempt}/{getattr(config, 'STASH_RETRIES', 3)}): {e}")
                if attempt == getattr(config, "STASH_RETRIES", 3): 
                    logger.error("Max retries reached for GraphQL request.")
                    return None
                await asyncio.sleep(1.0)

# Singleton instance
_manager = _StashConnectionManager()

async def test_stash_connection() -> bool:
    url = f"{config.get_stash_base()}{getattr(config, 'STASH_GRAPHQL_PATH', '/graphql')}"
    try:
        response = await _manager.client.post(url, headers=_manager.get_headers(), json={"query": "{ version { version } }"})
        response.raise_for_status()
        data = response.json()
        return bool("data" in data and "version" in data["data"])
    except Exception as e: 
        logger.debug(f"Stash connection test failed: {e}")
        return False

async def call_graphql(query: str, variables: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    return await _manager.execute(query, variables)

async def get_scene(scene_id: str) -> Optional[Dict[str, Any]]:
    """Uses DETAILED fields (includes Markers) because we are only asking for 1 scene."""
    data = await call_graphql(f"query FindScene($id: ID!) {{ findScene(id: $id) {{ {DETAILED_SCENE_FIELDS} }} }}", {"id": scene_id})
    return data["findScene"] if data and data.get("findScene") else None

async def fetch_scenes(filter_args: Dict[str, Any], page: int = 1, per_page: int = 50, scene_filter: Dict[str, Any] = None, ignore_sync_level: bool = False) -> Dict[str, Any]:
    """Uses BASE fields (NO Markers) to prevent Stash from choking on 50-100 items at once."""
    sf = scene_filter or {}
    if "title" in filter_args: sf["title"] = filter_args.pop("title")
    
    # Optional: Keep your ignore_sync_level logic if you had it implemented previously
    if not ignore_sync_level:
        sync_mode = getattr(config, "SYNC_LEVEL", "Everything")
        if sync_mode == "Organized": sf["organized"] = True
        elif sync_mode == "Tagged": sf["tags"] = {"modifier": "NOT_NULL"}
    
    query = f"query FindScenes($filter: FindFilterType, $scene_filter: SceneFilterType) {{ findScenes(filter: $filter, scene_filter: $scene_filter) {{ count scenes {{ {BASE_SCENE_FIELDS} }} }} }}"
    filter_args.update({"page": page, "per_page": per_page})
    
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
    data = await call_graphql("""query { findTags(filter: {per_page: -1, sort: "name", direction: ASC}, tag_filter: {scene_count: {value: 0, modifier: GREATER_THAN}}) { tags { id name } } }""")
    return data.get("findTags", {}).get("tags", []) if data else []

async def get_saved_filters() -> list:
    data = await call_graphql("""query { findSavedFilters(mode: SCENES) { id name find_filter { q sort direction } object_filter } }""")
    if data and data.get("findSavedFilters"): return data["findSavedFilters"]
    data_legacy = await call_graphql("""query { findSavedFilters(mode: SCENES) { id name filter find_filter { q sort direction } } }""")
    return data_legacy.get("findSavedFilters", []) if data_legacy else []

async def get_performer(performer_id: str):
    data = await call_graphql("""query FindPerformer($id: ID!) { findPerformer(id: $id) { id name image_path alias_list gender birthdate country ethnicity hair_color eye_color height_cm weight measurements piercings tattoos details fake_tits career_length penis_length circumcised } }""", {"id": performer_id})
    return data.get("findPerformer") if data else None

async def get_scene_sprite(scene_id: str) -> str:
    data = await call_graphql("""query($id: ID!) { findScene(id: $id) { paths { sprite } } }""", {"id": scene_id})
    return data.get("findScene", {}).get("paths", {}).get("sprite") if data else None

async def ensure_tags_exist(tag_names: list) -> list:
    """Responsibility: Match Jellyfin string tags to Stash Tag IDs, creating missing ones dynamically."""
    if not tag_names: return []
    
    existing_tags = await get_all_tags()
    tag_map = {t["name"].lower(): str(t["id"]) for t in existing_tags}
    
    final_ids = []
    for name in tag_names:
        clean_name = str(name).strip()
        if not clean_name: continue
        
        lower_name = clean_name.lower()
        if lower_name in tag_map:
            final_ids.append(tag_map[lower_name])
        else:
            logger.info(f"Creating new Stash tag: '{clean_name}'")
            res = await call_graphql(
                "mutation($name: String!) { tagCreate(input: {name: $name}) { id } }", 
                {"name": clean_name}
            )
            if res and res.get("tagCreate"):
                final_ids.append(str(res["tagCreate"]["id"]))
                
    return list(set(final_ids))

async def update_scene(update_input: dict) -> bool:
    """Responsibility: Submit the SceneUpdateInput payload to Stash."""
    query = """
    mutation SceneUpdate($input: SceneUpdateInput!) {
        sceneUpdate(input: $input) { id }
    }
    """
    result = await call_graphql(query, {"input": update_input})
    return result is not None and result.get("sceneUpdate") is not None