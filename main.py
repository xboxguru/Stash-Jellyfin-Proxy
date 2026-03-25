import os
import sys
import time
import asyncio
import logging
import socket
import json
from hypercorn.config import Config
from hypercorn.asyncio import serve
from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket
from logging.handlers import RotatingFileHandler
from starlette.middleware.cors import CORSMiddleware

# Import our custom modules
import config
from core import stash_client
from api.middleware import AuthenticationMiddleware
from api import ui_routes, auth_routes, library_routes, metadata_routes, stream_routes, userdata_routes, image_routes

# Configure Logging with Log Rotation (Max 5MB, keeps 2 backups)
if not os.path.exists(config.LOG_DIR):
    try:
        os.makedirs(config.LOG_DIR, exist_ok=True)
    except Exception as e:
        print(f"Warning: Could not create LOG_DIR {config.LOG_DIR}. Falling back to '.' - {e}")
        config.LOG_DIR = "."

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

logger = logging.getLogger("proxy_main")

# --- STATIC IP CACHER (Prevents UDP Blocking) ---
def get_local_ip():
    # 1. Prioritize explicitly configured Docker Host IP
    local_ip = getattr(config, "HOST_IP", "").strip()
    if not local_ip:
        # 2. Try to automatically detect the network IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            # 3. Fall back to the bind address
            local_ip = getattr(config, "PROXY_BIND", "127.0.0.1")
            if local_ip == "0.0.0.0":
                local_ip = "127.0.0.1"
    return local_ip

CACHED_LOCAL_IP = get_local_ip()

async def dummy_websocket(websocket: WebSocket):
    """Holds WebSocket connections open to prevent strict clients (Wholphin) from panicking."""
    await websocket.accept()
    try:
        while True:
            # We just silently catch and ignore any heartbeats the client sends
            await websocket.receive_text()
    except Exception:
        pass

routes = [
    # --- UI Routes (Proxy Web Dashboard) ---
    Route("/", ui_routes.serve_index, methods=["GET"]),
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
    Route("/api/cache/increment", ui_routes.api_increment_cache_version, methods=["POST"]),
    
    # --- System & Auth ---
    Route("/system/info/public", auth_routes.endpoint_system_info_public, methods=["GET"]),
    Route("/public/system/info", auth_routes.endpoint_system_info_public, methods=["GET"]),
    Route("/system/info", auth_routes.endpoint_system_info, methods=["GET"]),
    Route("/system/ping", auth_routes.endpoint_system_ping, methods=["GET", "POST"]),
    Route("/users/public", auth_routes.endpoint_public_users, methods=["GET"]),
    Route("/users/authenticatebyname", auth_routes.endpoint_authenticate_by_name, methods=["POST"]),
    Route("/users/{user_id}", auth_routes.endpoint_user, methods=["GET"]),
    Route("/users", auth_routes.endpoint_users, methods=["GET"]),
    Route("/quickconnect/enabled", auth_routes.endpoint_quickconnect_enabled, methods=["GET"]),
    Route("/quickconnect/initiate", auth_routes.endpoint_quickconnect_initiate, methods=["POST"]),
    Route("/branding/configuration", auth_routes.endpoint_branding_configuration, methods=["GET"]),
    
    # --- Specific Library Routes (Static paths MUST come before variable paths) ---
    Route("/userviews", library_routes.endpoint_views, methods=["GET"]),
    Route("/users/{user_id}/views", library_routes.endpoint_views, methods=["GET"]),
    Route("/library/virtualfolders", library_routes.endpoint_virtual_folders, methods=["GET"]),
    Route("/users/{user_id}/items/resume", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/useritems/resume", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/users/{user_id}/items/latest", library_routes.endpoint_latest, methods=["GET"]),
    Route("/items/latest", library_routes.endpoint_latest, methods=["GET"]),
    Route("/items/suggestions", library_routes.endpoint_empty_list, methods=["GET"]),
    
    # --- ANDROID TV & WHOLPHIN STUBS ---
    Route("/sessions/capabilities", auth_routes.endpoint_system_ping, methods=["POST"]),
    Route("/movies/recommendations", library_routes.endpoint_empty_array, methods=["GET"]),
    Route("/items/filters", library_routes.endpoint_filters, methods=["GET"]),
    Route("/items/filters2", library_routes.endpoint_filters, methods=["GET"]),
    Route("/mediasegments/{item_id}", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/shows/nextup", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/genres", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/users/{user_id}/genres", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/tags", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/users/{user_id}/tags", metadata_routes.endpoint_tags, methods=["GET"]),
    Route("/years", metadata_routes.endpoint_years, methods=["GET"]),
    Route("/studios", metadata_routes.endpoint_studios, methods=["GET"]),
    
    # Generic item listing
    Route("/items", library_routes.endpoint_items, methods=["GET"]),
    Route("/users/{user_id}/items", library_routes.endpoint_items, methods=["GET"]),

    # --- Item Detail & Image Routes ---
    Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_item_details, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}", metadata_routes.endpoint_delete_item, methods=["DELETE"]),
    Route("/items/{item_id}", metadata_routes.endpoint_item_details, methods=["GET"]),
    Route("/items/{item_id}", metadata_routes.endpoint_delete_item, methods=["DELETE"]),
    
    # Pre-Flight Detail Stubs
    Route("/users/{user_id}/items/{item_id}/thememedia", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/themesongs", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/similar", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/specialfeatures", library_routes.endpoint_empty_array, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/intros", library_routes.endpoint_empty_list, methods=["GET"]),
    
    Route("/items/{item_id}/thememedia", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/items/{item_id}/themesongs", library_routes.endpoint_theme_songs, methods=["GET"]),
    Route("/items/{item_id}/similar", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/items/{item_id}/specialfeatures", library_routes.endpoint_empty_array, methods=["GET"]),
    Route("/items/{item_id}/intros", library_routes.endpoint_empty_list, methods=["GET"]),
    
    # Catch ALL Images (Primary, Backdrop, Thumb, Logo)
    Route("/items/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/items/{item_id}/images/{image_type}/{image_index}", image_routes.endpoint_item_image, methods=["GET"]),
    
    # Fladder Specific: Catch User Profile Avatar Image & Prefixed item images
    Route("/users/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/images/{image_type}", image_routes.endpoint_item_image, methods=["GET"]),
    Route("/videos/{item_id}/trickplay/{width}/{file_name}", image_routes.endpoint_trickplay_image, methods=["GET"]),
    
    # --- Playback ---
    Route("/users/{user_id}/items/{item_id}/playbackinfo", stream_routes.endpoint_playback_info, methods=["POST", "GET"]),
    Route("/items/{item_id}/playbackinfo", stream_routes.endpoint_playback_info, methods=["POST", "GET"]),
    
    # HLS Segment Pipeline
    Route("/videos/{item_id}/hls/{segment}", stream_routes.endpoint_hls_segment, methods=["GET"]),
    Route("/videos/{item_id}/master.m3u8", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    Route("/videos/{item_id}/main.m3u8", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    
    Route("/videos/{item_id}/stream.mp4", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    Route("/videos/{item_id}/stream", stream_routes.endpoint_stream, methods=["GET", "HEAD"]),
    Route("/sessions/playing", userdata_routes.endpoint_sessions_playing, methods=["POST"]),
    Route("/sessions/playing/progress", userdata_routes.endpoint_sessions_playing, methods=["POST"]),
    Route("/sessions/playing/stopped", userdata_routes.endpoint_sessions_stopped, methods=["POST"]),
    
    # --- Watched Status ---
    Route("/users/{user_id}/playeditems/{item_id}", userdata_routes.endpoint_mark_played, methods=["POST"]),
    Route("/users/{user_id}/playeditems/{item_id}", userdata_routes.endpoint_mark_unplayed, methods=["DELETE"]),
    Route("/userplayeditems/{item_id}", userdata_routes.endpoint_mark_played, methods=["POST"]),
    Route("/userplayeditems/{item_id}", userdata_routes.endpoint_mark_unplayed, methods=["DELETE"]),
    Route("/useritems/{item_id}/userdata", userdata_routes.endpoint_update_userdata, methods=["POST"]),
    Route("/users/{user_id}/items/{item_id}/userdata", userdata_routes.endpoint_update_userdata, methods=["POST"]),
    
    # --- Favorites ---
    Route("/users/{user_id}/favoriteitems/{item_id}", userdata_routes.endpoint_mark_favorite, methods=["POST"]),
    Route("/users/{user_id}/favoriteitems/{item_id}", userdata_routes.endpoint_unmark_favorite, methods=["DELETE"]),
    Route("/userfavoriteitems/{item_id}", userdata_routes.endpoint_mark_favorite, methods=["POST"]),
    Route("/userfavoriteitems/{item_id}", userdata_routes.endpoint_unmark_favorite, methods=["DELETE"]),
    
    Route("/displaypreferences/{display_id}", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/users/{user_id}/displaypreferences/{display_id}", library_routes.endpoint_empty_list, methods=["GET"]),
    Route("/users/{user_id}/policy", auth_routes.endpoint_user, methods=["GET"]),
    Route("/users/{user_id}/configuration", auth_routes.endpoint_user, methods=["GET"]),

    # --- Downloads ---
    Route("/items/{item_id}/download", stream_routes.endpoint_stream, methods=["GET"]),
    Route("/users/{user_id}/items/{item_id}/download", stream_routes.endpoint_stream, methods=["GET"]),

    # --- Logs ---
    Route("/clientlog/document", auth_routes.endpoint_client_log, methods=["POST"]),

    # --- Websocket ---
    WebSocketRoute("/socket", dummy_websocket),
]

# Initialize the Starlette App
app = Starlette(debug=(config.LOG_LEVEL == "DEBUG"), routes=routes)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Wrap the app in our custom Security Bouncer (AuthenticationMiddleware)
# This ensures every request (except UI) requires the PROXY_API_KEY
asgi_app = AuthenticationMiddleware(app)

class JellyfinDiscoveryProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport
        logger.info("📡 UDP Auto-Discovery Service listening on port 7359")

    def datagram_received(self, data, addr):
        message = data.decode('utf-8', errors='ignore').strip()
        if "who is" in message.lower():
            response = {
                "Address": f"http://{CACHED_LOCAL_IP}:{getattr(config, 'PROXY_PORT', 8096)}",
                "EndpointAddress": f"http://{CACHED_LOCAL_IP}:{getattr(config, 'PROXY_PORT', 8096)}",
                "Id": getattr(config, "SERVER_ID", "stash-proxy-unique-id"),
                "Name": getattr(config, "SERVER_NAME", "Stash Proxy"),
                "Version": "10.11.6" 
            }
            logger.debug(f"Answering discovery ping from {addr[0]} with cached IP {CACHED_LOCAL_IP}")
            self.transport.sendto(json.dumps(response).encode('utf-8'), addr)

async def run_server():
    """Configures and runs the Hypercorn ASGI server."""
    hypercorn_config = Config()
    hypercorn_config.bind = [f"{config.PROXY_BIND}:{config.PROXY_PORT}"]
    if hasattr(config, "UI_PORT") and config.UI_PORT != config.PROXY_PORT:
        hypercorn_config.bind.append(f"{config.PROXY_BIND}:{config.UI_PORT}")
    
    logger.info("=" * 50)
    logger.info(f"🚀 Starting Stash-Jellyfin Proxy v2")
    logger.info(f"🌐 Proxy Listening on: {config.PROXY_BIND}:{config.PROXY_PORT}")
    if config.PROXY_API_KEY:
        logger.info(f"🔑 Proxy API Key Loaded: {config.PROXY_API_KEY}")
    logger.info("=" * 50)

    # Validate Stash Connection on Startup
    stash_online = await stash_client.test_stash_connection()
    if not stash_online:
        logger.warning("⚠️ Warning: Stash is unreachable! Proxy will start, but clients will fail to load data.")
    
    loop = asyncio.get_running_loop()
    try:
        discovery_transport, _ = await loop.create_datagram_endpoint(
            lambda: JellyfinDiscoveryProtocol(),
            local_addr=('0.0.0.0', 7359),
            allow_broadcast=True
        )
    except Exception as e:
        logger.error(f"Failed to bind UDP Discovery on port 7359: {e}")
        discovery_transport = None
        
    shutdown_event = asyncio.Event()
    
    async def watch_for_restart():
        """Background task that watches for the UI restart flag and safely prunes zombie streams."""
        import state
        while True:
            if ui_routes.RESTART_REQUESTED:
                logger.info("Restart flag detected. Initiating graceful shutdown...")
                shutdown_event.set()
                break
                
            current_time = time.time()
            if hasattr(state, "active_streams"):
                original_count = len(state.active_streams)
                surviving_streams = []
                
                for s in state.active_streams:
                    # If it hasn't pinged in 15 minutes (900s), treat it as a crashed client
                    if current_time - s.get("last_ping", s.get("started", current_time)) >= 900:
                        item_id = s.get("item_id", "")
                        if item_id.startswith("scene-"):
                            raw_id = item_id.replace("scene-", "")
                            
                            try:
                                last_ticks = float(s.get("last_ticks", 0))
                                runtime_ticks = float(s.get("runtime_ticks", 0))
                                
                                # Salvage the resume point or watch state before deleting it from memory!
                                if runtime_ticks > 0:
                                    pct = last_ticks / runtime_ticks
                                    if 0.01 < pct < 0.90:
                                        resume_seconds = last_ticks / 10000000.0
                                        logger.info(f"🧟 Salvaging resume point ({resume_seconds}s) for crashed stream {s.get('id')}")
                                        # UPDATED TO USE STASH_CLIENT:
                                        asyncio.create_task(stash_client.update_resume_time(raw_id, resume_seconds))
                                    elif pct >= 0.90:
                                        logger.info(f"🧟 Salvaging watch status for crashed stream {s.get('id')}")
                                        # UPDATED TO USE STASH_CLIENT:
                                        asyncio.create_task(stash_client.increment_play_count(raw_id))
                            except Exception as e:
                                logger.error(f"Failed to salvage zombie stream data: {e}")
                    else:
                        surviving_streams.append(s)
                        
                state.active_streams = surviving_streams
                if len(state.active_streams) < original_count:
                    logger.info(f"🧹 Pruned {original_count - len(state.active_streams)} zombie streams from memory.")
                    
            await asyncio.sleep(60)

    watch_task = asyncio.create_task(watch_for_restart())
    await serve(asgi_app, hypercorn_config, shutdown_trigger=shutdown_event.wait)
    watch_task.cancel()
    
    if discovery_transport:
        discovery_transport.close()

def main():
    """Main execution block with restart handling."""
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Server stopped by user (CTRL+C).")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal server error: {e}")
        sys.exit(1)
        
    if ui_routes.RESTART_REQUESTED:
        logger.info("Executing in-place server restart...")
        time.sleep(1) 
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            logger.error(f"Failed to execute restart: {e}")
            sys.exit(1)

if __name__ == "__main__":
    main()