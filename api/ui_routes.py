import os
import logging
import datetime
import secrets
from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse
from starlette.requests import Request
import config
import state  # Required for IP and Session management
from core import stash_client

logger = logging.getLogger(__name__)
RESTART_REQUESTED = False

async def serve_index(request: Request):
    """Serves the main HTML page and replaces template variables."""
    # Prevent the Proxy Dashboard from loading on the Jellyfin API port
    proxy_port = getattr(config, "PROXY_PORT", 8096)
    if request.url.port == proxy_port and proxy_port != getattr(config, "UI_PORT", 8097):
        return PlainTextResponse("Stash-Jellyfin Proxy API is running. (No Web Client available)", status_code=200)

    template_path = os.path.join(config.SCRIPT_DIR, "templates", "index.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            html_content = f.read()
            
        server_name = getattr(config, "SERVER_NAME", "Stash Media Server")
        html_content = html_content.replace("{{SERVER_NAME}}", server_name)
        html_content = html_content.replace("{{VERSION}}", "Modular v2")
        
        return HTMLResponse(html_content)
    except Exception as e:
        logger.error(f"Failed to load index.html: {e}")
        return HTMLResponse(f"<h1>Error loading UI</h1><p>{e}</p>", status_code=500)

async def api_get_config(request: Request):
    """Exposes all configuration and state data to the Web UI."""
    config_data = {
        "STASH_URL": getattr(config, "STASH_URL", ""),
        "STASH_API_KEY": getattr(config, "STASH_API_KEY", ""),
        "PROXY_API_KEY": getattr(config, "PROXY_API_KEY", ""),
        "PROXY_BIND": getattr(config, "PROXY_BIND", "0.0.0.0"),
        "PROXY_PORT": getattr(config, "PROXY_PORT", 8096),
        "UI_PORT": getattr(config, "UI_PORT", 8097),
        "SJS_USER": getattr(config, "SJS_USER", ""),
        "SJS_PASSWORD": getattr(config, "SJS_PASSWORD", ""),
        "REQUIRE_AUTH_FOR_CONFIG": getattr(config, "REQUIRE_AUTH_FOR_CONFIG", False),
        "SERVER_NAME": getattr(config, "SERVER_NAME", "Stash Media Server"),
        "SERVER_ID": getattr(config, "SERVER_ID", ""),
        "RECENT_DAYS": getattr(config, "RECENT_DAYS", 14),
        "ENABLE_FILTERS": getattr(config, "ENABLE_FILTERS", True),
        "ENABLE_TAG_FILTERS": getattr(config, "ENABLE_TAG_FILTERS", False),
        "ENABLE_IMAGE_RESIZE": getattr(config, "ENABLE_IMAGE_RESIZE", True),
        "ENABLE_ALL_TAGS": getattr(config, "ENABLE_ALL_TAGS", False),
        "STASH_VERIFY_TLS": getattr(config, "STASH_VERIFY_TLS", False),
        "IMAGE_VERSION": getattr(config, "IMAGE_VERSION", 0),
        "TAG_GROUPS": getattr(config, "TAG_GROUPS", []),
        "LATEST_GROUPS": getattr(config, "LATEST_GROUPS", ["Scenes"]),
        "SYNC_LEVEL": getattr(config, "SYNC_LEVEL", "Everything"),
        "STASH_GRAPHQL_PATH": getattr(config, "STASH_GRAPHQL_PATH", "/graphql"),
        "STASH_TIMEOUT": getattr(config, "STASH_TIMEOUT", 30),
        "STASH_RETRIES": getattr(config, "STASH_RETRIES", 3),
        "DEFAULT_PAGE_SIZE": getattr(config, "DEFAULT_PAGE_SIZE", 50),
        "MAX_PAGE_SIZE": getattr(config, "MAX_PAGE_SIZE", 200),
        "LOG_LEVEL": getattr(config, "LOG_LEVEL", "INFO"),
        "LOG_DIR": getattr(config, "LOG_DIR", "/config"),
        "LOG_FILE": getattr(config, "LOG_FILE", "stash_jellyfin_proxy.log"),
        "LOG_MAX_SIZE_MB": getattr(config, "LOG_MAX_SIZE_MB", 10),
        "LOG_BACKUP_COUNT": getattr(config, "LOG_BACKUP_COUNT", 3),
        "BAN_THRESHOLD": getattr(config, "BAN_THRESHOLD", 10),
        "BAN_WINDOW_MINUTES": getattr(config, "BAN_WINDOW_MINUTES", 15),
        "BANNED_IPS": list(getattr(config, "BANNED_IPS", set())),
        "AUTHENTICATED_IPS": list(getattr(state, "authenticated_ips", set())),
    }
    
    return JSONResponse({
        "config": config_data,
        "env_fields": getattr(config, "env_overrides", []),
        "defined_fields": list(getattr(config, "config_defined_keys", set()))
    })

async def api_post_config(request: Request):
    """Saves settings sent from the UI to memory and persistent storage."""
    try:
        data = await request.json()
        
        # 1. Check for restart triggers
        restart_triggers = ["PROXY_PORT", "PROXY_BIND", "UI_PORT", "LOG_LEVEL"]
        needs_restart = any(str(data.get(k)) != str(getattr(config, k, "")) for k in restart_triggers if k in data)
        
        # 2. Apply settings to memory and specific persistent files
        for key, value in data.items():
            if key == "AUTHENTICATED_IPS":
                # Handle the trusted IP JSON storage
                new_ips = set(value) if isinstance(value, list) else set()
                state.authenticated_ips = new_ips
                state.save_auth_ips(new_ips)
            else:
                setattr(config, key, value)
            
        # 3. Save standard config to .conf
        config.save_config()
        
        if needs_restart:
            global RESTART_REQUESTED 
            RESTART_REQUESTED = True
            
        return JSONResponse({"status": "success", "needs_restart": needs_restart})
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

async def api_get_logs(request: Request):
    """Parses the log file for the UI viewer."""
    log_path = os.path.join(getattr(config, "LOG_DIR", "."), getattr(config, "LOG_FILE", "stash_jellyfin_proxy.log"))
    entries = []
    clear_time = getattr(state, "log_clear_time", "")
    limit = int(request.query_params.get("limit", 200))
    
    try:
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding="utf-8") as f:
                lines = f.readlines()[-limit:]
                for line in lines:
                    parts = line.split("] ", 1)
                    if len(parts) == 2 and " [" in parts[0]:
                        timestamp, level = parts[0].split(" [")
                        if clear_time and timestamp < clear_time: continue
                        entries.append({
                            "timestamp": timestamp.strip(),
                            "level": level.strip(),
                            "message": parts[1].strip()
                        })
    except Exception as e:
        entries.append({"timestamp": "", "level": "ERROR", "message": f"Log error: {e}"})
    return JSONResponse({"entries": entries})

async def api_clear_logs(request: Request):
    state.log_clear_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return JSONResponse({"status": "cleared"})

async def api_get_status(request: Request):
    stash_ok = stash_client.test_stash_connection()
    return JSONResponse({
        "running": True, 
        "stashConnected": stash_ok,
        "stashVersion": "0.27+",
        "version": "Modular v2",
        "uptime": 0
    })

async def api_restart(request: Request):
    global RESTART_REQUESTED
    RESTART_REQUESTED = True
    return JSONResponse({"status": "restarting"})

async def api_get_streams(request: Request):
    return JSONResponse({"streams": getattr(state, "active_streams", [])})

async def api_get_stats(request: Request):
    """Combines Stash library counts with Proxy usage stats."""
    stash_stats = stash_client.get_stash_stats()
    current_day = datetime.datetime.now().strftime("%Y-%m-%d")
    
    if getattr(state, "day_tracker", "") != current_day:
        state.stats["streams_today"] = 0
        state.stats["unique_ips_today"] = set()
        state.day_tracker = current_day

    top_played_list = sorted(state.stats["top_played"].values(), key=lambda x: x["count"], reverse=True)[:5]

    return JSONResponse({
        "stash": {
            "scenes": stash_stats.get("scene_count", 0),
            "performers": stash_stats.get("performer_count", 0),
            "studios": stash_stats.get("studio_count", 0),
            "tags": stash_stats.get("tag_count", 0),
            "groups": stash_stats.get("group_count", 0)
        },
        "proxy": {
            "streams_today": state.stats["streams_today"],
            "total_streams": state.stats["total_streams"],
            "unique_ips_today": len(state.stats["unique_ips_today"]),
            "auth_success": state.stats["auth_success"],
            "auth_failed": state.stats["auth_failed"],
            "top_played": top_played_list
        }
    })

async def api_reset_stats(request: Request):
    """Resets the usage statistics in the memory bank."""
    state.stats["streams_today"] = 0
    state.stats["total_streams"] = 0
    state.stats["auth_success"] = 0
    state.stats["auth_failed"] = 0
    state.stats["top_played"] = {}
    return JSONResponse({"status": "success"})

async def api_auth_check(request: Request):
    if not getattr(config, "REQUIRE_AUTH_FOR_CONFIG", False):
        return JSONResponse({"authenticated": True})
    return JSONResponse({"authenticated": True})

async def api_login(request: Request):
    try: data = await request.json()
    except: data = {}
    if data.get("username") == config.SJS_USER and data.get("password") == config.SJS_PASSWORD:
        token = secrets.token_hex(32)
        state.ui_sessions.add(token)
        response = JSONResponse({"status": "success"})
        response.set_cookie(key="ui_session", value=token, httponly=True, max_age=86400, samesite="lax")
        return response
    return JSONResponse({"error": "Invalid credentials"}, status_code=401)

async def api_logout(request: Request):
    token = request.cookies.get("ui_session")
    if token in state.ui_sessions: state.ui_sessions.remove(token)
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("ui_session")
    return response

async def api_increment_cache_version(request):
    """Increments the global cache version to force clients to redownload metadata and images."""
    config.CACHE_VERSION = getattr(config, "CACHE_VERSION", 0) + 1
    config.save_config()
    return JSONResponse({
        "message": f"Global Cache Version bumped to v{config.CACHE_VERSION}. Tunarr and ErsatzTV will rebuild their libraries on the next sync."
    })