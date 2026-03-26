import time
import os
import json
import config

ui_sessions = set()

# --- FILE PATHS ---
if os.path.exists("/.dockerenv") and os.path.isdir("/config"):
    AUTH_IPS_FILE = "/config/authenticated_IPs.json"
    STATS_FILE = "/config/stats.json"
else:
    AUTH_IPS_FILE = os.path.join(config.SCRIPT_DIR, "authenticated_IPs.json")
    STATS_FILE = os.path.join(config.SCRIPT_DIR, "stats.json")

# --- AUTH IPs PERSISTENCE ---
def load_auth_ips():
    if os.path.exists(AUTH_IPS_FILE):
        try:
            with open(AUTH_IPS_FILE, 'r') as f:
                data = json.load(f)
                
                # Scenario 1: It's a dictionary {"ips": ...}
                if isinstance(data, dict):
                    ips = data.get("ips", {})
                    # If it's already the new timestamped format
                    if isinstance(ips, dict): 
                        return ips
                    # If it's the wrapped legacy list {"ips": ["1.2.3.4"]}
                    elif isinstance(ips, list): 
                        return {ip: time.time() for ip in ips}
                
                # Scenario 2: It's a raw legacy list ["1.2.3.4", "5.6.7.8"]
                elif isinstance(data, list):
                    return {ip: time.time() for ip in data}
                    
        except Exception as e:
            print(f"Error loading authenticated IPs: {e}")
    return {}

def save_auth_ips(ips_dict):
    try:
        with open(AUTH_IPS_FILE, 'w') as f:
            json.dump({"ips": ips_dict}, f)
    except Exception as e:
        print(f"Error saving authenticated IPs: {e}")

authenticated_ips = load_auth_ips()

# --- STATS PERSISTENCE ---
stats = {
    "streams_today": 0,
    "total_streams": 0,
    "unique_ips_today": set(),
    "auth_success": 0,
    "auth_failed": 0,
    "top_played": {}, 
}

def load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, 'r') as f:
                data = json.load(f)
                if "top_played" in data: stats["top_played"] = data["top_played"]
                if "total_streams" in data: stats["total_streams"] = data["total_streams"]
        except Exception as e:
            print(f"Error loading stats: {e}")

def save_stats():
    try:
        with open(STATS_FILE, 'w') as f:
            json.dump({
                "top_played": stats["top_played"],
                "total_streams": stats["total_streams"]
            }, f)
    except Exception as e:
        print(f"Error saving stats: {e}")

load_stats()

day_tracker = time.strftime("%Y-%m-%d")
active_streams = []
log_clear_time = ""