import os
import sys
import time
import asyncio
import logging
import socket
import subprocess
from contextlib import asynccontextmanager
from hypercorn.config import Config
from hypercorn.asyncio import serve
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.websockets import WebSocket
from logging.handlers import RotatingFileHandler
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles
import mimetypes

mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')

import config
import state
from core import stash_client
from core.udp_discovery import JellyfinDiscoveryProtocol
from api.middleware import AuthenticationMiddleware
from api import ui_routes, auth_routes, library_routes, metadata_routes, stream_routes, userdata_routes, image_routes

if not os.path.exists(config.LOG_DIR):
    try: os.makedirs(config.LOG_DIR, exist_ok=True)
    except Exception: config.LOG_DIR = "."

# --- BEGIN CUSTOM LOGGING INJECTION ---
NOTICE_LEVEL_NUM = 15
TRACE_LEVEL_NUM = 5

logging.addLevelName(NOTICE_LEVEL_NUM, "NOTICE")
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")

def notice(self, message, *args, **kws):
    if self.isEnabledFor(NOTICE_LEVEL_NUM):
        self._log(NOTICE_LEVEL_NUM, message, args, **kws)

def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)

logging.Logger.notice = notice
logging.Logger.trace = trace

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        RotatingFileHandler(
            os.path.join(config.LOG_DIR, config.LOG_FILE),
            maxBytes=getattr(config, "LOG_MAX_SIZE_MB", 5) * 1024 * 1024,
            backupCount=getattr(config, "LOG_BACKUP_COUNT", 2),
            encoding="utf-8"
        )
    ]
)

logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("proxy_main")

def _get_local_ip():
    local_ip = getattr(config, "HOST_IP", "").strip()
    if not local_ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = getattr(config, "PROXY_BIND", "127.0.0.1")
            if local_ip == "0.0.0.0": local_ip = "127.0.0.1"
    return local_ip

CACHED_LOCAL_IP = _get_local_ip()

async def dummy_websocket(websocket: WebSocket):
    await websocket.accept()
    logger.debug("WebSocket connection opened to prevent strict client panic.")
    try:
        while True: await websocket.receive_text()
    except Exception: pass

async def root_router(request: Request):
    ui_port = getattr(config, "UI_PORT", 8097)
    if request.url.port == ui_port:
        return await ui_routes.serve_index(request)
    return RedirectResponse(url="/web/index.html", status_code=302)

routes = [
    Route("/", root_router, methods=["GET"]),
    Route("/favicon.ico", lambda r: RedirectResponse(url="/web/favicon.ico", status_code=302), methods=["GET"]),
    
    Route("/api/config", ui_routes.api_get_config, methods=["GET"]),
    Route("/api/config", ui_routes.api_post_config, methods=["POST"]),
    Route("/api/logs", ui_routes.api_get_logs, methods=["GET"]),
    Route("/api/logs/clear", ui_routes.api_clear_logs, methods=["POST"]),
    Route("/api/status", ui_routes.api_get_status, methods=["GET"]),
    Route("/api/restart", ui_routes.api_restart, methods=["POST"]),
    Route("/api/streams", ui_routes.api_get_streams, methods=["GET"]),
    Route("/api/stats", ui_routes.api_get_stats, methods=["GET"]),
    Route("/api/stats/reset", ui_routes.api_reset_stats, methods=["POST"]),
    Route("/api/auth/check", ui_routes.api_auth_check, methods=["GET"]),
    Route("/api/auth/login", ui_routes.api_login, methods=["POST"]),
    Route("/api/auth/logout", ui_routes.api_logout, methods=["POST"]),
    Route("/api/auth/dynamic_ips/{ip}", ui_routes.api_prune_dynamic_ip, methods=["DELETE"]),
    Route("/api/cache/increment", ui_routes.api_increment_cache_version, methods=["POST"]),
    Route("/api/cache/clear", ui_routes.api_clear_cache, methods=["POST"]),
    Route("/api/stats/top_played", ui_routes.api_clear_top_played, methods=["DELETE"]),
    Route("/api/stats/top_played/{item_id}", ui_routes.api_remove_top_played_item, methods=["DELETE"]),
    Route('/api/quickconnect/authorize', auth_routes.endpoint_quickconnect_authorize, methods=['POST']),
    Route("/api/sysinfo", ui_routes.api_get_sysinfo, methods=["GET"]),
    
    Route("/system/info/public", auth_routes.endpoint_system_info_public, methods=["GET"]),
    Route("/public/system/info", auth_routes.endpoint_system_info_public, methods=["GET"]),
    Route("/system/info", auth_routes.endpoint_system_info, methods=["GET"]),
    Route("/system/ping", auth_routes.endpoint_system_ping, methods=["GET", "POST"]),
    Route("/users/public", auth_routes.endpoint_public_users, methods=["GET"]),
    Route("/users/authenticatebyname", auth_routes.endpoint_authenticate_by_name, methods=["POST"]),
    Route("/users/{user_id}", auth_routes.endpoint_user, methods=["GET"]),
    Route("/users", auth_routes.endpoint_users, methods=["GET"]),
    Route('/users/authenticatewithquickconnect', auth_routes.endpoint_authenticate_by_quickconnect, methods=['POST']),
    Route('/quickconnect/enabled', auth_routes.endpoint_quickconnect_enabled, methods=['GET']),
    Route('/quickconnect/initiate', auth_routes.endpoint_quickconnect_initiate, methods=['GET', 'POST']),
    Route('/quickconnect/connect', auth_routes.endpoint_quickconnect_connect, methods=['GET']),
    Route("/branding/configuration", auth_routes.endpoint_branding_configuration, methods=["GET"]),
    
    Route("/userviews", library_routes.endpoint_views, methods=["GET"]),
    Route("/users/{user_id}/views", library_routes.endpoint_views, methods=["GET"]),
    Route("/library/virtualfolders", library_routes.endpoint_virtual_folders, methods=["GET"]),
    Route("/users/{user_id}/items/resume", library_routes.endpoint_resume, methods=["GET"]),
    Route("/useritems/resume", library_routes.endpoint_resume, methods=["GET"]),
    Route("/users/{user_id}/items/latest", library_routes.endpoint_latest, methods=["GET"]),
    Route("/items/latest", library_routes.endpoint_latest, methods=["GET"]),
    Route("/items/suggestions", library_routes.endpoint_empty_list, methods=["GET"]),
    
    Route("/sessions/capabilities", auth_routes.endpoint_system_ping, methods=["POST"]),
    Route("/movies/recommendations", library_routes.endpoint_empty_array, methods=["GET"]),
    Route("/items/filters", library_routes.endpoint_filters, methods=["GET"]),
    Route("/items/filters2", library_routes.endpoint_filters, methods=["GET"]),
    Route("/mediasegments/{item_id}", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/shows/{series_id}/episodes", library_routes.endpoint_shows_episodes, methods=["GET"]),
    Route("/shows/nextup", library_routes.endpoint_next_up, methods=["GET"]),
    Route("/genres", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/users/{user_id}/genres", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/tags", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/users/{user_id}/tags", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/years", metadata_routes.endpoint_years, methods=["GET"]),
    Route("/studios", metadata_routes.endpoint_studios, methods=["GET"]),
    Route("/persons", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/artists", library_routes.endpoint_empty_list, methods=["GET"]),
    
    Route("/items", library_routes.endpoint_items, methods=["GET"]),
    Route("/users/{user_id}/items", library_routes.endpoint_items, methods=["GET"]),
    Route("/search/hints", library_routes.endpoint_search_hints, methods=["GET"]),

    Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_item_details, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_delete_item, methods=["DELETE"]),
    Route("/items/{item_id}", metadata_routes.endpoint_item_details, methods=["GET"]),
    Route("/items/{item_id}", metadata_routes.endpoint_delete_item, methods=["DELETE"]),
    Route("/items/{item_id}/metadataeditor", metadata_routes.endpoint_metadata_editor, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/metadataeditor", metadata_routes.endpoint_metadata_editor, methods=["GET"]),
    
    Route("/items/{item_id}", metadata_routes.endpoint_update_item, methods=["POST"]),
    Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_update_item, methods=["POST"]),
    Route("/items/{item_id}/images", metadata_routes.endpoint_item_images_info, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/images", metadata_routes.endpoint_item_images_info, methods=["GET"]),
    
    Route("/users/{user_id}/items/{item_id}/thememedia", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/themesongs", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/similar", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/specialfeatures", library_routes.endpoint_empty_array, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/intros", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/items/{item_id}/thememedia", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/items/{item_id}/themesongs", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/items/{item_id}/similar", library_routes.endpoint_similar_items, methods=["GET"]),
    Route("/items/{item_id}/specialfeatures", library_routes.endpoint_empty_array, methods=["GET"]),
    Route("/items/{item_id}/intros", library_routes.endpoint_empty_list, methods=["GET"]),
    
    Route("/items/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/items/{item_id}/images/{image_type}/{image_index}", image_routes.endpoint_item_image, methods=["GET"]),
    
    Route("/users/{user_id}/items/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/images/{image_type}/{image_index}", image_routes.endpoint_item_image, methods=["GET"]),
    
    Route("/users/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/videos/{item_id}/trickplay/{width}/{file_name}", image_routes.endpoint_trickplay_image, methods=["GET"]),
    
    Route("/users/{user_id}/items/{item_id}/playbackinfo", stream_routes.endpoint_playback_info, methods=["POST", "GET"]),
    Route("/items/{item_id}/playbackinfo", stream_routes.endpoint_playback_info, methods=["POST", "GET"]),
    
    Route("/videos/{item_id}/hls/{segment}", stream_routes.endpoint_hls_segment, methods=["GET"]),
    Route("/videos/{item_id}/master.m3u8", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    Route("/videos/{item_id}/main.m3u8", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    Route("/videos/{item_id}/stream.mp4", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    Route("/videos/{item_id}/stream", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    
    Route("/sessions/playing", userdata_routes.endpoint_sessions_playing, methods=["POST"]),
    Route("/sessions/playing/progress", userdata_routes.endpoint_sessions_playing, methods=["POST"]),
    Route("/sessions/playing/stopped", userdata_routes.endpoint_sessions_stopped, methods=["POST"]),
    
    Route("/users/{user_id}/playeditems/{item_id}", userdata_routes.endpoint_mark_played, methods=["POST"]),
    Route("/users/{user_id}/playeditems/{item_id}", userdata_routes.endpoint_mark_unplayed, methods=["DELETE"]),
    Route("/userplayeditems/{item_id}", userdata_routes.endpoint_mark_played, methods=["POST"]),
    Route("/userplayeditems/{item_id}", userdata_routes.endpoint_mark_unplayed, methods=["DELETE"]),
    Route("/useritems/{item_id}/userdata", userdata_routes.endpoint_update_userdata, methods=["POST"]),
    Route("/users/{user_id}/items/{item_id}/userdata", userdata_routes.endpoint_update_userdata, methods=["POST"]),
    
    Route("/users/{user_id}/favoriteitems/{item_id}", userdata_routes.endpoint_mark_favorite, methods=["POST"]),
    Route("/users/{user_id}/favoriteitems/{item_id}", userdata_routes.endpoint_unmark_favorite, methods=["DELETE"]),
    Route("/userfavoriteitems/{item_id}", userdata_routes.endpoint_mark_favorite, methods=["POST"]),
    Route("/userfavoriteitems/{item_id}", userdata_routes.endpoint_unmark_favorite, methods=["DELETE"]),
    
    Route("/displaypreferences/{display_id}", library_routes.endpoint_display_preferences, methods=["GET", "POST"]),
    Route("/users/{user_id}/displaypreferences/{display_id}", library_routes.endpoint_display_preferences, methods=["GET", "POST"]),
    Route("/users/{user_id}/policy", auth_routes.endpoint_user, methods=["GET"]),
    Route("/users/{user_id}/configuration", auth_routes.endpoint_user, methods=["GET"]),

    Route("/items/{item_id}/download", stream_routes.endpoint_stream, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/download", stream_routes.endpoint_stream, methods=["GET"]),

    Route("/clientlog/document", auth_routes.endpoint_client_log, methods=["POST"]),

    WebSocketRoute("/socket", dummy_websocket),

    Mount("/web", app=StaticFiles(directory="jellyfin-web", html=True), name="jellyfin-web"),
    Route("/{path:path}", auth_routes.endpoint_blackhole, methods=["GET", "POST", "OPTIONS", "DELETE"]),
]

@asynccontextmanager
async def lifespan(app):
    yield
    logger.info("Shutting down global HTTP connection pools...")
    await stash_client._manager.client.aclose()
    await stream_routes.stream_client.aclose()
    await image_routes.image_client.aclose()

app = Starlette(debug=(config.LOG_LEVEL == "DEBUG"), routes=routes, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

asgi_app = AuthenticationMiddleware(app)

async def background_pruner():
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            
            timeout = getattr(config, "AUTH_IP_TIMEOUT_MINUTES", 60)
            if timeout > 0 and hasattr(state, "authenticated_ips") and isinstance(state.authenticated_ips, dict):
                expired_ips = [ip for ip, ts in state.authenticated_ips.items() if now - ts > (timeout * 60)]
                if expired_ips:
                    for ip in expired_ips: del state.authenticated_ips[ip]
                    logger.trace(f"Pruned {len(expired_ips)} expired IP authentications.")
                    if hasattr(state, "save_auth_ips"): state.save_auth_ips(state.authenticated_ips)

            retention = getattr(config, "TOP_PLAYED_RETENTION_DAYS", 0)
            if retention > 0 and "top_played" in state.stats:
                expired_scenes = [sid for sid, data in state.stats["top_played"].items() if now - data.get("last_played", now) > (retention * 86400)]
                if expired_scenes:
                    for sid in expired_scenes: del state.stats["top_played"][sid]
                    logger.trace(f"Pruned {len(expired_scenes)} expired top played records.")
                    if hasattr(state, "save_stats"): state.save_stats()
                    
        except Exception as e:
            logger.error(f"Background pruner encountered an error: {e}")

async def continuous_preheater():
    import json
    from core import stash_client
    logger.notice("Starting continuous cache pre-heater for primary libraries (5-minute interval).")
    
    # We must formulate the EXACT dictionaries that library_routes generates to match the cache keys
    filter_str = json.dumps({"direction": "ASC", "sort": "title"}, sort_keys=True)
    scene_filter_all = json.dumps({}, sort_keys=True)
    scene_filter_org = json.dumps({"organized": True}, sort_keys=True)
    scene_filter_tag = json.dumps({"tags": {"modifier": "NOT_NULL"}}, sort_keys=True)
    
    while True:
        try:
            # Gather fires all 3 requests at the exact same time
            await asyncio.gather(
                stash_client.fetch_lightweight_index(filter_str, scene_filter_all),
                stash_client.fetch_lightweight_index(filter_str, scene_filter_org),
                stash_client.fetch_lightweight_index(filter_str, scene_filter_tag)
            )
            logger.trace("Primary libraries pre-heated successfully.")
        except Exception as e:
            logger.warning(f"Cache pre-heater encountered an issue: {e}")
            
        # Sleep for exactly 300 seconds (the aiocache TTL). 
        # When it wakes up, the cache will have just expired, guaranteeing a fresh pull!
        await asyncio.sleep(300)

async def run_server():
    hypercorn_config = Config()
    hypercorn_config.bind = [f"{config.PROXY_BIND}:{config.PROXY_PORT}"]
    hypercorn_config.graceful_timeout = 3.0 
    
    if hasattr(config, "UI_PORT") and config.UI_PORT != config.PROXY_PORT:
        hypercorn_config.bind.append(f"{config.PROXY_BIND}:{config.UI_PORT}")
    
    logger.notice("=" * 50)
    logger.notice(f"Stash-Jellyfin Proxy v2")
    logger.notice(f"Proxy API: {config.PROXY_BIND}:{config.PROXY_PORT}")
    if config.PROXY_API_KEY: logger.notice(f"Proxy API Key Loaded")
    logger.notice("=" * 50)

    stash_online = await stash_client.test_stash_connection()
    if not stash_online:
        logger.warning("Stash is unreachable! Proxy will start, but clients will fail to load data.")
    else:
        logger.notice("Connected to Stash successfully.")
    
    loop = asyncio.get_running_loop()
    try:
        discovery_transport, _ = await loop.create_datagram_endpoint(
            lambda: JellyfinDiscoveryProtocol(CACHED_LOCAL_IP),
            local_addr=('0.0.0.0', 7359),
            allow_broadcast=True
        )
    except Exception as e:
        logger.error(f"Failed to bind UDP Discovery on port 7359: {e}")
        discovery_transport = None
        
    shutdown_trigger_event = asyncio.Event()

    async def watch_for_restart():
        while True:
            try:
                if getattr(ui_routes, "RESTART_REQUESTED", False):
                    logger.info("Restart flag detected. Initiating graceful shutdown...")
                    shutdown_trigger_event.set()
                    break
                await userdata_routes.prune_and_salvage_zombie_streams()
                state.clean_expired_quick_connects()
            except Exception as e:
                logger.error(f"Watch-for-restart encountered an error: {e}")
            await asyncio.sleep(60)

    watch_task = asyncio.create_task(watch_for_restart())
    prune_task = asyncio.create_task(background_pruner())
    preheat_task = asyncio.create_task(continuous_preheater())
    
    await serve(asgi_app, hypercorn_config, shutdown_trigger=shutdown_trigger_event.wait)
    
    watch_task.cancel()
    prune_task.cancel()
    preheat_task.cancel()
    if discovery_transport: discovery_transport.close()

def main():
    try: 
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Server stopped by user (CTRL+C).")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal server error: {e}")
        sys.exit(1)
        
    if getattr(ui_routes, "RESTART_REQUESTED", False):
        logger.info("Executing server restart...")
        time.sleep(1) 
        try: 
            logging.shutdown()
            
            script_path = os.path.abspath(__file__)
            args = [sys.executable, script_path] + sys.argv[1:]
            
            os.execv(sys.executable, args)
            
        except Exception as e:
            print(f"Failed to execute restart: {e}")
            os._exit(1)

if __name__ == "__main__":
    main()