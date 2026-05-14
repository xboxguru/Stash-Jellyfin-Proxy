import os
import logging
import datetime, time
import secrets
import psutil
import ipaddress
from starlette.responses import JSONResponse, HTMLResponse, PlainTextResponse
from starlette.requests import Request
from starlette.templating import Jinja2Templates
import config
import state  # Required for IP and Session management
from core import stash_client

logger = logging.getLogger(__name__)
RESTART_REQUESTED = False

templates = Jinja2Templates(directory=os.path.join(config.SCRIPT_DIR, "templates"))
SENSITIVE_CONFIG_KEYS = {"STASH_API_KEY", "PROXY_API_KEY", "SJS_PASSWORD"}
REDACTED_SENTINEL = "***redacted***"

def _ip_matches(client_ip: str, entries: list) -> bool:
    try:
        addr = ipaddress.ip_address(client_ip)
        for entry in entries:
            try:
                if addr in ipaddress.ip_network(str(entry), strict=False):
                    return True
            except ValueError:
                continue
    except ValueError:
        pass
    return False

def _get_client_ip(request: Request) -> str:
    direct_ip = request.client.host if request.client else "127.0.0.1"
    if not bool(getattr(config, "TRUST_PROXY_HEADERS", False)):
        return direct_ip
    trusted_proxy_ips = set(getattr(config, "TRUSTED_PROXY_IPS", []) or [])
    if trusted_proxy_ips and direct_ip not in trusted_proxy_ips:
        return direct_ip
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return direct_ip

def _check_login_rate_limit(client_ip: str) -> bool:
    if not hasattr(state, "login_attempts") or not isinstance(getattr(state, "login_attempts", None), dict):
        state.login_attempts = {}
    now = time.time()
    window_seconds = max(1, int(getattr(config, "AUTH_RATE_LIMIT_WINDOW_MINUTES", 15))) * 60
    max_attempts = max(1, int(getattr(config, "AUTH_RATE_LIMIT_MAX_ATTEMPTS", 10)))
    attempts = [ts for ts in state.login_attempts.get(client_ip, []) if now - ts <= window_seconds]
    state.login_attempts[client_ip] = attempts
    return len(attempts) < max_attempts

def _record_login_failure(client_ip: str):
    if not hasattr(state, "login_attempts") or not isinstance(getattr(state, "login_attempts", None), dict):
        state.login_attempts = {}
    state.login_attempts.setdefault(client_ip, []).append(time.time())

async def serve_index(request: Request):
    """Serves the modular HTML dashboard using Jinja2."""
    proxy_port = getattr(config, "PROXY_PORT", 8096)
    if request.url.port == proxy_port and proxy_port != getattr(config, "UI_PORT", 8097):
        return PlainTextResponse("Stash-Jellyfin Proxy API is running. (No Web Client available)", status_code=200)

    context = {
        "SERVER_NAME": getattr(config, "SERVER_NAME", "Stash Media Server"),
        "VERSION": getattr(config, "APP_VERSION", "v2.1-dev"),
        "config": config
    }

    return templates.TemplateResponse(request, "index.html", context)

async def api_get_config(request: Request):
    """Exposes all configuration and state data to the Web UI dynamically."""
    config_data = {k: getattr(config, k) for k in dir(config) if k.isupper() and not k.startswith("_")}
    
    for k, v in config_data.items():
        if isinstance(v, set):
            config_data[k] = list(v)
        if k in SENSITIVE_CONFIG_KEYS and config_data.get(k):
            config_data[k] = REDACTED_SENTINEL
            
    # Safely fetch the dynamic auto-whitelist
    raw_dynamic = getattr(state, "authenticated_ips", {})
    if isinstance(raw_dynamic, set): 
        raw_dynamic = {ip: time.time() for ip in raw_dynamic}
        
    # --- THE FIX: Filter out statically configured IPs ---
    static_ips = getattr(config, "AUTHENTICATED_IPS", [])
    dynamic_ips = {ip: ts for ip, ts in raw_dynamic.items() if ip not in static_ips}
    # -----------------------------------------------------
    
    return JSONResponse({
        "config": config_data,
        "dynamic_ips": dynamic_ips,
        "env_fields": getattr(config, "env_overrides", []),
        "defined_fields": list(getattr(config, "config_defined_keys", set()))
    })

async def api_prune_dynamic_ip(request: Request):
    """Allows the UI to manually revoke an auto-whitelisted IP."""
    ip_to_prune = request.path_params.get("ip")
    if hasattr(state, "authenticated_ips") and isinstance(state.authenticated_ips, dict):
        if ip_to_prune in state.authenticated_ips:
            del state.authenticated_ips[ip_to_prune]
            if hasattr(state, "save_auth_ips"):
                state.save_auth_ips(state.authenticated_ips)
    return JSONResponse({"status": "success"})

async def api_post_config(request: Request):
    """Saves settings sent from the UI to memory and persistent storage."""
    try:
        data = await request.json()
        
        # 1. Check for restart triggers
        restart_triggers = ["PROXY_PORT", "PROXY_BIND", "UI_PORT", "LOG_LEVEL"]
        needs_restart = any(str(data.get(k)) != str(getattr(config, k, "")) for k in restart_triggers if k in data)
        
        # 2. Apply settings to memory and specific persistent files
        allowed_config_keys = set(getattr(config, "_supported_keys", []))
        for key, value in data.items():
            if key in allowed_config_keys:
                # Keep existing secrets when UI submits redacted placeholders.
                if key in SENSITIVE_CONFIG_KEYS and str(value).strip() == REDACTED_SENTINEL:
                    continue
                # UI sends typed JSON (arrays as lists, numbers as ints, bools as bools).
                # Only stringify for _coerce_config_value when the value needs normalization
                # (e.g. LOG_LEVEL uppercase, path normalization, set conversion, list->set).
                if isinstance(value, list):
                    # BANNED_IPS expects a set; all other list keys expect a list — handle both
                    coerced = set(value) if key == "BANNED_IPS" else value
                elif isinstance(value, (bool, int, float)):
                    coerced = value
                else:
                    coerced_str = config._coerce_config_value(key, str(value))
                    coerced = coerced_str if coerced_str is not None else value
                setattr(config, key, coerced)
            else:
                logger.warning(f"Rejected unsupported config update key: {key}")
            
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
    stash_ok = await stash_client.test_stash_connection()
    return JSONResponse({
        "running": True, 
        "stashConnected": stash_ok,
        "stashVersion": "0.27+",
        "version": getattr(config, "APP_VERSION", "v2.1-dev"),
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
    stash_stats = await stash_client.get_stash_stats()
    current_day = datetime.datetime.now().strftime("%Y-%m-%d")
    
    if getattr(state, "day_tracker", "") != current_day:
        state.stats["streams_today"] = 0
        state.stats["unique_ips_today"] = set()
        state.day_tracker = current_day

    top_stash_scenes = await stash_client.fetch_top_played_scenes(limit=100)
    top_played_list = []
    for s in top_stash_scenes:
        # Safely extract performers into a comma-separated string
        performers = ", ".join([p["name"] for p in s.get("performers", [])]) if s.get("performers") else "Unknown"
        top_played_list.append({
            "id": s["id"],
            "title": s.get("title") or f"Scene {s['id']}",
            "performer": performers,
            "count": s.get("play_count", 1)
        })

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
    token = request.cookies.get("ui_session")
    is_authenticated = bool(token and token in state.ui_sessions)
    return JSONResponse({"authenticated": is_authenticated}, status_code=200 if is_authenticated else 401)

async def api_login(request: Request):
    try: data = await request.json()
    except: data = {}
    client_ip = _get_client_ip(request)
    allowed_ui_ips = getattr(config, "UI_ALLOWED_IPS", [])
    if allowed_ui_ips and not _ip_matches(client_ip, allowed_ui_ips):
        logger.warning(f"Blocked login attempt from non-allowlisted IP {client_ip}")
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not _check_login_rate_limit(client_ip):
        logger.warning(f"Rate-limited login attempts from {client_ip}")
        return JSONResponse({"error": "Too many attempts, try again later."}, status_code=429)
    provided_user = str(data.get("username", data.get("Username", ""))).strip()
    provided_pass = str(data.get("password", data.get("Password", data.get("Pw", ""))))
    expected_user = str(getattr(config, "SJS_USER", "")).strip()
    expected_pass = str(getattr(config, "SJS_PASSWORD", ""))

    if bool(getattr(config, "REQUIRE_AUTH_FOR_CONFIG", False)) and (not expected_user or not expected_pass):
        logger.error("UI login rejected: REQUIRE_AUTH_FOR_CONFIG is enabled but SJS_USER/SJS_PASSWORD are not configured.")
        return JSONResponse({"error": "UI auth is enabled but credentials are not configured."}, status_code=503)

    if provided_user.lower() == expected_user.lower() and provided_pass == expected_pass:
        token = secrets.token_hex(32)
        state.ui_sessions.add(token)
        if hasattr(state, "login_attempts") and client_ip in state.login_attempts:
            state.login_attempts.pop(client_ip, None)
        response = JSONResponse({"status": "success"})
        response.set_cookie(
            key="ui_session",
            value=token,
            httponly=True,
            max_age=86400,
            samesite="lax",
            secure=bool(getattr(config, "TRUST_PROXY_HEADERS", False))
        )
        return response
    _record_login_failure(client_ip)
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

async def api_clear_cache(request: Request):
    """Flushes the in-memory metadata cache so fresh data is pulled from Stash immediately."""
    await stash_client.clear_all_caches()
    logger.info("Metadata cache cleared via UI request.")
    return JSONResponse({"status": "success", "message": "Cache cleared. Fresh metadata will be fetched from Stash on the next request."})

async def api_clear_top_played(request: Request):
    import state
    state.stats["top_played"] = {}
    if hasattr(state, "save_stats"): state.save_stats()
    return JSONResponse({"success": True})

async def api_remove_top_played_item(request: Request):
    import state
    item_id = request.path_params.get("item_id")
    if item_id and item_id in state.stats["top_played"]:
        del state.stats["top_played"][item_id]
        if hasattr(state, "save_stats"): state.save_stats()
    return JSONResponse({"success": True})

async def api_get_sysinfo(request: Request):
    """Fetches real-time hardware resource usage for the dashboard."""
    process = psutil.Process(os.getpid())
    
    # System Metrics (interval=0 is non-blocking)
    sys_cpu = psutil.cpu_percent(interval=0.0)
    sys_mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    # Proxy Specific Metrics
    # Divide by cpu_count so a maxed single core shows as exactly what it takes from the total system
    proxy_cpu = process.cpu_percent(interval=0.0) / psutil.cpu_count() 
    proxy_mem_mb = process.memory_info().rss / (1024 * 1024)
    
    uptime_sec = time.time() - process.create_time()
    
    return JSONResponse({
        "system": {
            "cpu_percent": round(sys_cpu, 1),
            "ram_percent": round(sys_mem.percent, 1),
            "ram_used_mb": round(sys_mem.used / (1024*1024), 1),
            "ram_total_mb": round(sys_mem.total / (1024*1024), 1),
            "disk_percent": round(disk.percent, 1)
        },
        "proxy": {
            "cpu_percent": round(proxy_cpu, 1),
            "ram_used_mb": round(proxy_mem_mb, 1),
            "uptime_sec": int(uptime_sec)
        }
    })