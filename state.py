import time
import os
import json
import logging
import config

logger = logging.getLogger(__name__)

ui_sessions = set()
quick_connect_sessions = {}

if os.path.exists("/.dockerenv") and os.path.isdir("/config"):
    AUTH_IPS_FILE = "/config/authenticated_IPs.json"
    STATS_FILE = "/config/stats.json"
    PREFS_FILE = "/config/display_preferences.json"
else:
    AUTH_IPS_FILE = os.path.join(config.SCRIPT_DIR, "authenticated_IPs.json")
    STATS_FILE = os.path.join(config.SCRIPT_DIR, "stats.json")
    PREFS_FILE = os.path.join(config.SCRIPT_DIR, "display_preferences.json")

def _load_json(filepath: str, default_val: dict) -> dict:
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading state file {filepath}: {e}", exc_info=True)
    return default_val

def _save_json(filepath: str, data: dict):
    try:
        with open(filepath, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Error saving state file {filepath}: {e}", exc_info=True)

display_preferences = {}

def load_prefs():
    global display_preferences
    display_preferences = _load_json(PREFS_FILE, {})
    logger.debug(f"Loaded {len(display_preferences)} display preferences.")

def save_prefs():
    _save_json(PREFS_FILE, display_preferences)
    logger.debug("Saved display preferences to disk.")

def load_auth_ips():
    data = _load_json(AUTH_IPS_FILE, {})
    if isinstance(data, dict):
        ips = data.get("ips", {})
        if isinstance(ips, dict): return ips
        elif isinstance(ips, list): return {ip: time.time() for ip in ips}
    elif isinstance(data, list):
        return {ip: time.time() for ip in data}
    return {}

def save_auth_ips(ips_dict):
    if isinstance(ips_dict, set):
        ips_dict = {ip: time.time() for ip in ips_dict}
    _save_json(AUTH_IPS_FILE, {"ips": ips_dict})
    logger.debug(f"Saved {len(ips_dict)} authenticated IPs to disk.")

authenticated_ips = load_auth_ips()

stats = {
    "streams_today": 0,
    "total_streams": 0,
    "unique_ips_today": set(),
    "auth_success": 0,
    "auth_failed": 0,
    "top_played": {}, 
}

def load_stats():
    data = _load_json(STATS_FILE, {})
    if "top_played" in data: stats["top_played"] = data["top_played"]
    if "total_streams" in data: stats["total_streams"] = data["total_streams"]
    logger.debug("Server stats loaded.")

def save_stats():
    _save_json(STATS_FILE, {
        "top_played": stats["top_played"],
        "total_streams": stats["total_streams"]
    })

def clean_expired_quick_connects():
    current_time = time.time()
    expired = [s for s, data in quick_connect_sessions.items() if current_time - data['timestamp'] > 900]
    for s in expired:
        del quick_connect_sessions[s]
        logger.debug(f"Purged expired Quick Connect session: {s}")

load_stats()
load_prefs()

day_tracker = time.strftime("%Y-%m-%d")
active_streams = []
log_clear_time = ""