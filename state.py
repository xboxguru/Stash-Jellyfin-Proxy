import time
import secrets

ui_sessions = set()  # Tracks active web UI login tokens

# NEW: Tracks IPs that have successfully used the API key
authenticated_ips = set()

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