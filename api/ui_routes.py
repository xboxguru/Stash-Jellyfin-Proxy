import os
import logging
import state
from starlette.responses import JSONResponse, HTMLResponse
from starlette.requests import Request
import config
from core import stash_client

logger = logging.getLogger(__name__)
RESTART_REQUESTED = False

async def serve_index(request: Request):
    """Serves the main HTML page and replaces template variables."""
    template_path = os.path.join(config.SCRIPT_DIR, "templates", "index.html")
    try:
        with open(template_path, "r", encoding="utf-8") as f:
            html_content = f.read()
            
        # Replace the ugly brackets with the actual server name!
        server_name = getattr(config, "SERVER_NAME", "Stash Media Server")
        html_content = html_content.replace("{{SERVER_NAME}}", server_name)
        html_content = html_content.replace("{{VERSION}}", "Modular v2")
        
        return HTMLResponse(html_content)
    except Exception as e:
        logger.error(f"Failed to load index.html: {e}")
        return HTMLResponse(f"<h1>Error loading UI</h1><p>{e}</p>", status_code=500)

async def api_get_config(request: Request):
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
        "defined_fields": list(getattr(config, "config_defined_keys", set())) # <-- This fixes the blank fields!
    })

async def api_post_config(request: Request):
    """Saves settings sent from the UI to memory and the file."""
    try:
        data = await request.json()
        
        # 1. Check if we need to warn the user to restart BEFORE applying changes
        restart_triggers = [
            "PROXY_PORT", "PROXY_BIND", "UI_PORT", 
            "LOG_LEVEL", "LOG_DIR", "LOG_FILE", "LOG_MAX_SIZE_MB", "LOG_BACKUP_COUNT"
        ]
        
        needs_restart = any(str(data.get(k)) != str(getattr(config, k, "")) for k in restart_triggers if k in data)
        
        # 2. Apply new settings to memory
        for key, value in data.items():
            if key == "AUTHENTICATED_IPS":
                import state
                if isinstance(value, str):
                    new_ips = set(ip.strip() for ip in value.split(",") if ip.strip())
                elif isinstance(value, list):
                    new_ips = set(value)
                else:
                    new_ips = set()
                state.authenticated_ips = new_ips
                state.save_auth_ips(new_ips)
            else:
                setattr(config, key, value)
            
        # 3. Save everything cleanly to the file
        config.save_config()
        
        # 4. Trigger the restart flag if necessary
        if needs_restart:
            global RESTART_REQUESTED 
            RESTART_REQUESTED = True
            
        return JSONResponse({"status": "success", "needs_restart": needs_restart})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

async def api_get_logs(request: Request):
    """Parses the log file and hides entries before the clear timestamp."""
    import state
    log_path = os.path.join(getattr(config, "LOG_DIR", "."), getattr(config, "LOG_FILE", "stash_jellyfin_proxy.log"))
    entries = []
    clear_time = getattr(state, "log_clear_time", "")
    
    # Extract the limit from the URL, default to 200
    limit = int(request.query_params.get("limit", 200))
    
    try:
        if os.path.exists(log_path):
            with open(log_path, 'r', encoding="utf-8") as f:
                lines = f.readlines()[-limit:] # Use the dynamic limit here
                for line in lines:
                    parts = line.split("] ", 1)
                    if len(parts) == 2 and " [" in parts[0]:
                        timestamp, level = parts[0].split(" [")
                        
                        if clear_time and timestamp < clear_time:
                            continue
                            
                        entries.append({
                            "timestamp": timestamp.strip(),
                            "level": level.strip(),
                            "message": parts[1].strip()
                        })
    except Exception as e:
        entries.append({"timestamp": "", "level": "ERROR", "message": f"Could not read logs: {e}"})

    return JSONResponse({"entries": entries})

async def api_clear_logs(request: Request):
    """Sets a timestamp marker to hide old logs from the UI view."""
    import state, datetime
    # Get current time in the exact format the python logger uses
    state.log_clear_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return JSONResponse({"status": "cleared"})

async def api_get_status(request: Request):
    """Checks the connection to Stash and returns the proxy status."""
    try:
        stash_ok = stash_client.test_stash_connection()
    except Exception as e:
        logger.error(f"Status check failed: {e}")
        stash_ok = False
        
    return JSONResponse({
        "running": True, 
        "stashConnected": stash_ok,
        "stashVersion": "0.27+", # The JS looks for this
        "version": "Modular v2",
        "uptime": 0 # Prevents JS errors
    })

async def api_restart(request: Request):
    global RESTART_REQUESTED
    RESTART_REQUESTED = True
    return JSONResponse({"status": "restarting"})

async def api_get_streams(request: Request):
    """Fetches the active streams from the memory bank for the UI."""
    import state
    # Ensure active_streams exists as a list
    if not hasattr(state, "active_streams"):
        state.active_streams = []
        
    return JSONResponse({"streams": state.active_streams})

async def api_get_stats(request: Request):
    """Combines Stash library counts with internal Proxy usage stats."""
    import state, time
    
    # 1. Get Stash Data
    stash_stats = stash_client.get_stash_stats()
    
    # 2. Reset "Today" stats if it's a new day
    current_day = time.strftime("%Y-%m-%d")
    if getattr(state, "day_tracker", "") != current_day:
        state.stats["streams_today"] = 0
        state.stats["unique_ips_today"] = set()
        state.day_tracker = current_day

    # 3. Format the top played list for the UI
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

async def api_auth_check(request: Request):
    """The frontend calls this to see if it should show the login screen."""
    # If security is off, always return authenticated
    if not getattr(config, "REQUIRE_AUTH_FOR_CONFIG", False):
        return JSONResponse({"authenticated": True})
        
    # Middleware already checked the cookie by the time we get here
    return JSONResponse({"authenticated": True})

async def api_login(request: Request):
    """Validates credentials and issues a secure session cookie."""
    try:
        data = await request.json()
    except Exception:
        data = {}
        
    username = data.get("username", "")
    password = data.get("password", "")
    
    expected_user = getattr(config, "SJS_USER", "")
    expected_pass = getattr(config, "SJS_PASSWORD", "")
    
    # Check credentials
    if username == expected_user and password == expected_pass:
        import state, secrets
        # Generate a secure 64-character random session token
        token = secrets.token_hex(32)
        state.ui_sessions.add(token)
        
        response = JSONResponse({"status": "success"})
        # Set the token as a secure, HTTP-only cookie valid for 24 hours
        response.set_cookie(key="ui_session", value=token, httponly=True, max_age=86400, samesite="lax")
        return response
        
    return JSONResponse({"error": "Invalid credentials"}, status_code=401)

async def api_logout(request: Request):
    """Destroys the current session."""
    import state
    token = request.cookies.get("ui_session")
    if token in getattr(state, "ui_sessions", set()):
        state.ui_sessions.remove(token)
        
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("ui_session")
    return response

async def api_increment_image_version(request: Request):
    """Increments the cache busting tag and saves it properly."""
    try:
        current = int(getattr(config, "IMAGE_VERSION", 0))
        config.IMAGE_VERSION = current + 1
        
        # Trigger the new dynamic save engine
        config.save_config() 
        
        return JSONResponse({"status": "success", "new_version": config.IMAGE_VERSION})
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    
async def api_reset_stats(request: Request):
    """Resets the internal proxy usage statistics."""
    import state
    state.stats["streams_today"] = 0
    state.stats["total_streams"] = 0
    state.stats["auth_success"] = 0
    state.stats["auth_failed"] = 0
    state.stats["top_played"] = {}
    return JSONResponse({"status": "success"})