import time
import secrets
import os
import json
import config

ui_sessions = set()  # Tracks active web UI login tokens

# --- JSON PERSISTENCE FOR TRUSTED IPs ---
# Determine the correct path based on Docker environment
if os.path.exists("/.dockerenv") and os.path.isdir("/config"):
    AUTH_IPS_FILE = "/config/authenticated_IPs.json"
else:
    AUTH_IPS_FILE = os.path.join(config.SCRIPT_DIR, "authenticated_IPs.json")

def load_auth_ips():
    """Loads trusted IPs from the JSON file on boot."""
    if os.path.exists(AUTH_IPS_FILE):
        try:
            with open(AUTH_IPS_FILE, 'r') as f:
                data = json.load(f)
                return set(data.get("ips", []))
        except Exception as e:
            print(f"Error loading authenticated IPs: {e}")
    return set()

def save_auth_ips(ips_set):
    """Saves the current trusted IPs to the JSON file."""
    try:
        with open(AUTH_IPS_FILE, 'w') as f:
            json.dump({"ips": list(ips_set)}, f)
    except Exception as e:
        print(f"Error saving authenticated IPs: {e}")

# NEW: Tracks IPs that have successfully used the API key (Persisted)
authenticated_ips = load_auth_ips()

# Proxy Memory Bank for Statistics
stats = {
    "streams_today": 0,
    "total_streams": 0,
    "unique_ips_today": set(),
    "auth_success": 0,
    "auth_failed": 0,
    "top_played": {},  # Format: {"scene_id": {"title": "X", "performer": "Y", "count": 1}}
}

# Track the current day so we can reset "today" stats at midnight
day_tracker = time.strftime("%Y-%m-%d")

# Active streams placeholder
active_streams = []

log_clear_time = ""