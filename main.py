import os
import sys
import time
import asyncio
import logging
from hypercorn.config import Config
from hypercorn.asyncio import serve
from starlette.applications import Starlette
from starlette.routing import Route
from logging.handlers import RotatingFileHandler

# Import our custom modules
import config
from api.middleware import AuthenticationMiddleware
from api import jellyfin_routes
from api import ui_routes

# Configure Logging with Log Rotation (Max 5MB, keeps 2 backups)
# Create log directory if it doesn't exist to prevent silent write failures
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

# Define the URL Routes
routes = [
    # --- Web UI Routes ---
    Route("/", ui_routes.serve_index, methods=["GET"]),
    Route("/api/config", ui_routes.api_get_config, methods=["GET"]),
    Route("/api/config", ui_routes.api_post_config, methods=["POST"]),
    Route("/api/status", ui_routes.api_get_status, methods=["GET"]),
    Route("/api/streams", ui_routes.api_get_streams, methods=["GET"]),
    Route("/api/stats", ui_routes.api_get_stats, methods=["GET"]),
    Route("/api/logs", ui_routes.api_get_logs, methods=["GET"]),
    Route("/api/restart", ui_routes.api_restart, methods=["POST"]),
    Route("/api/logs/clear", ui_routes.api_clear_logs, methods=["POST"]),
    Route("/api/auth/check", ui_routes.api_auth_check, methods=["GET"]),
    Route("/api/login", ui_routes.api_login, methods=["POST"]),
    Route("/api/logout", ui_routes.api_logout, methods=["POST"]),
    Route("/api/increment_image_version", ui_routes.api_increment_image_version, methods=["POST"]),

    # --- Jellyfin API Routes ---
    # Jellyfin clients request data using these standard endpoints
    Route("/Items", jellyfin_routes.endpoint_items, methods=["GET"]),
    Route("/Users/{user_id}/Items", jellyfin_routes.endpoint_items, methods=["GET"]),
    Route("/Users/{user_id}/Views", jellyfin_routes.endpoint_views, methods=["GET"]),
    Route("/Library/VirtualFolders", jellyfin_routes.endpoint_views, methods=["GET"]),
    Route("/Users/{user_id}/Views", jellyfin_routes.endpoint_views, methods=["GET"]),
    Route("/Library/VirtualFolders", jellyfin_routes.endpoint_virtual_folders, methods=["GET"]),
    Route("/system/info/public", jellyfin_routes.endpoint_system_info_public, methods=["GET"]),
    Route("/Items/{item_id}", jellyfin_routes.endpoint_item_details, methods=["GET"]),
    Route("/Users/{user_id}/Items/{item_id}", jellyfin_routes.endpoint_item_details, methods=["GET"]),
    Route("/Items/{item_id}/PlaybackInfo", jellyfin_routes.endpoint_playback_info, methods=["GET"]),
    Route("/Items/{item_id}/Images/{image_type}", jellyfin_routes.endpoint_item_image, methods=["GET"]),
    Route("/Studios", jellyfin_routes.endpoint_studios),
    Route("/Items/RemoteSearch/Studios", jellyfin_routes.endpoint_studios),
    
    # Playback Reporting Routes
    Route("/Sessions/Playing", jellyfin_routes.endpoint_sessions_playing, methods=["POST"]),
    Route("/Sessions/Playing/Progress", jellyfin_routes.endpoint_sessions_playing, methods=["POST"]),
    Route("/Sessions/Playing/Stopped", jellyfin_routes.endpoint_sessions_stopped, methods=["POST"]),
    # Authentication & User Mocking Routes
    Route("/Users/AuthenticateByName", jellyfin_routes.endpoint_authenticate_by_name, methods=["POST"]),
    Route("/Users", jellyfin_routes.endpoint_users, methods=["GET"]),
    Route("/Users/{user_id}", jellyfin_routes.endpoint_user, methods=["GET"]),
    Route("/System/Info", jellyfin_routes.endpoint_system_info, methods=["GET"]),
    Route("/QuickConnect/Enabled", jellyfin_routes.endpoint_quickconnect_enabled, methods=["GET"]),
]

# Initialize the Starlette App
app = Starlette(debug=(config.LOG_LEVEL == "DEBUG"), routes=routes)

# Wrap the app in our custom Security Bouncer (AuthenticationMiddleware)
# This ensures every request (except UI) requires the PROXY_API_KEY
asgi_app = AuthenticationMiddleware(app)

async def run_server():
    """Configures and runs the Hypercorn ASGI server."""
    hypercorn_config = Config()
    
    # Bind the ports (Proxy Port for ErsatzTV, UI Port for your browser)
    hypercorn_config.bind = [f"{config.PROXY_BIND}:{config.PROXY_PORT}"]
    if hasattr(config, "UI_PORT") and config.UI_PORT != config.PROXY_PORT:
        hypercorn_config.bind.append(f"{config.PROXY_BIND}:{config.UI_PORT}")
    
    logger.info("=" * 50)
    logger.info(f"🚀 Starting Stash-Jellyfin Proxy v2")
    logger.info(f"🌐 Proxy Listening on: {config.PROXY_BIND}:{config.PROXY_PORT}")
    if config.PROXY_API_KEY:
        logger.info(f"🔑 Proxy API Key Loaded: {config.PROXY_API_KEY}")
    logger.info("=" * 50)
    
    shutdown_event = asyncio.Event()
    
    async def watch_for_restart():
        """Background task that watches for the UI restart flag."""
        while True:
            if ui_routes.RESTART_REQUESTED:
                logger.info("Restart flag detected. Initiating graceful shutdown...")
                shutdown_event.set()
                break
            await asyncio.sleep(1)

    # Start the watcher task alongside the server
    watch_task = asyncio.create_task(watch_for_restart())
    
    # Start Hypercorn
    await serve(asgi_app, hypercorn_config, shutdown_trigger=shutdown_event.wait)
    watch_task.cancel()

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
        
    # --- RESTART LOGIC ---
    # This block executes after the asyncio loop is gracefully shut down
    if ui_routes.RESTART_REQUESTED:
        logger.info("Executing server restart...")
        time.sleep(1)  # Brief pause to ensure ports are released
        
        # Detect if we are inside Unraid/Docker
        in_docker = os.path.exists("/.dockerenv") or config.CONFIG_FILE.startswith("/config")
        
        if in_docker:
            logger.info("Docker environment detected. Exiting script to allow Docker restart policy to reboot the container.")
            sys.exit(0)
        else:
            logger.info("Standalone environment detected. Restarting Python process in place.")
            os.execv(sys.executable, ['python3'] + sys.argv)

if __name__ == "__main__":
    main()