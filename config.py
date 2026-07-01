import os
import sys
import uuid
import socket
import logging

logger = logging.getLogger(__name__)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --- 1. BULLETPROOF FILE PATH ---
if os.path.exists("/.dockerenv") and os.path.isdir("/config"):
    CONFIG_FILE = "/config/stash_jellyfin_proxy.conf"
else:
    CONFIG_FILE = os.path.join(SCRIPT_DIR, "stash_jellyfin_proxy.conf")

# Default Configuration
APP_VERSION = os.getenv("APP_VERSION", "v2.1-dev")
STASH_URL = "https://stash:9999"
STASH_API_KEY = ""
PROXY_API_KEY = ""  
SYNC_LEVEL = "Everything"  
PROXY_BIND = "0.0.0.0"
PROXY_PORT = 8096
UI_PORT = 8097
HOST_IP = ""  
SJS_USER = ""
SJS_PASSWORD = ""
TAG_GROUPS = []
LATEST_GROUPS = ["Scenes"]
SERVER_NAME = "Stash Media Server"
SERVER_ID = ""
LIBRARY_TYPE = "movies"
RECENT_DAYS = 14
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
ENABLE_FILTERS = True
ENABLE_TAG_FILTERS = False
ENABLE_ALL_TAGS = False
REQUIRE_AUTH_FOR_CONFIG = True
FAVORITE_ACTION = "o_counter"
ALLOW_CLIENT_DELETION = "Disabled"
STASH_TIMEOUT = 30
STASH_RETRIES = 3
STASH_GRAPHQL_PATH = "/graphql"
STASH_VERIFY_TLS = False
LOG_DIR = "."
LOG_FILE = "stash_jellyfin_proxy.log"
LOG_LEVEL = "INFO"
LOG_MAX_SIZE_MB = 10
LOG_BACKUP_COUNT = 3
BANNED_IPS = set()
BAN_THRESHOLD = 10
BAN_WINDOW_MINUTES = 15
CACHE_VERSION = 0 
AUTH_IP_TIMEOUT_MINUTES = 60
TOP_PLAYED_RETENTION_DAYS = 0
AUTHENTICATED_IPS = []
TRUST_PROXY_HEADERS = False
TRUSTED_PROXY_IPS = []
CORS_ALLOWED_ORIGINS = []
CLIENT_LOG_MAX_BYTES = 1048576
UI_PUBLIC_STATUS_ENDPOINT = False
UI_ALLOWED_IPS = []
UI_CSRF_PROTECTION = True
AUTH_RATE_LIMIT_WINDOW_MINUTES = 15
AUTH_RATE_LIMIT_MAX_ATTEMPTS = 10
ENABLE_LIVE_TV = False
ENABLE_TUNARR = False
TUNER_M3U_URL = ""
TUNER_XMLTV_URL = ""
ENABLE_STASH_CHANNELS = False
STASH_SCHEDULE_DAYS = 7
STASH_KEEP_DAYS = 2
STASH_CHANNEL_START_NUMBER = 5001
ENABLE_SHORTS_CHANNEL = False
SHORTS_MAX_MINUTES = 5
FFMPEG_PATH = "ffmpeg"
LIVE_TV_IDLE_TIMEOUT = 60
LIVE_TV_HLS_LIST_SIZE = 15
LIVE_TV_SEG_RETENTION = 30
ENABLE_HANDY_SYNC = False
HANDY_SYNC_MODE = "auto"  # auto (follow Stash) | hosted (force HSSP/cloud) | local (force HSP/LAN)
HANDY_APPLICATION_ID = ""  # Handy REST API v3 ApplicationID (X-Api-Key); empty = fall back to v2 API
# HSP (local streaming) buffer tuning — advanced; HSP path only, HSSP ignores these.
HANDY_HSP_BUFFER_MIN_S = 30       # seconds of motion seeded on play/seek (the safety floor)
HANDY_HSP_BUFFER_MAX_S = 60       # seconds the buffer is topped up to on each poll
HANDY_HSP_POLL_INTERVAL_S = 15    # how often the refill task polls the device to top up (seconds)

config_defined_keys = set()
env_overrides = []

def normalize_path(path, default="/graphql"):
    if not path or not isinstance(path, str) or not path.strip(): return default
    p = path.strip()
    if not p.startswith('/'): p = '/' + p
    if len(p) > 1 and p.endswith('/'): p = p.rstrip('/')
    return p

def get_stash_base():
    """Returns the Stash URL stripped of trailing slashes."""
    return getattr(sys.modules[__name__], "STASH_URL", "http://localhost:9999").rstrip('/')

_cached_proxy_ip = None

def _detect_local_ip():
    """Best-effort LAN-reachable IP for this proxy: configured HOST_IP, else the source IP used
    to reach the internet, else the bind address. Cached after first call."""
    global _cached_proxy_ip
    if _cached_proxy_ip:
        return _cached_proxy_ip
    ip = getattr(sys.modules[__name__], "HOST_IP", "").strip()
    if not ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = getattr(sys.modules[__name__], "PROXY_BIND", "127.0.0.1")
            if ip == "0.0.0.0":
                ip = "127.0.0.1"
    _cached_proxy_ip = ip
    return ip

def get_proxy_base():
    """Returns the proxy's LAN-reachable base URL (http://<ip>:<PROXY_PORT>), for handing
    device-reachable URLs (e.g. the Handy funscript URL in LAN/direct mode) to external devices."""
    port = getattr(sys.modules[__name__], "PROXY_PORT", 8096)
    return f"http://{_detect_local_ip()}:{port}"

# --- 2. DYNAMIC SAVE FUNCTION ---
def save_config():
    keys_to_save = [
        "STASH_URL", "STASH_API_KEY", "PROXY_BIND", "PROXY_PORT", "UI_PORT", "HOST_IP", "PROXY_API_KEY",
        "SJS_USER", "SJS_PASSWORD", "SERVER_ID", "SERVER_NAME", "TAG_GROUPS", "LATEST_GROUPS",
        "STASH_TIMEOUT", "STASH_RETRIES", "STASH_GRAPHQL_PATH", "STASH_VERIFY_TLS",
        "SYNC_LEVEL", "ENABLE_FILTERS", "ENABLE_TAG_FILTERS", 
        "ENABLE_ALL_TAGS", "CACHE_VERSION", "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE",
        "REQUIRE_AUTH_FOR_CONFIG", "LOG_DIR", "LOG_FILE", 
        "LOG_LEVEL", "LOG_MAX_SIZE_MB", "LOG_BACKUP_COUNT", "BAN_THRESHOLD", 
        "BAN_WINDOW_MINUTES", "BANNED_IPS", "RECENT_DAYS", "FAVORITE_ACTION", "ALLOW_CLIENT_DELETION",
        "AUTH_IP_TIMEOUT_MINUTES", "TOP_PLAYED_RETENTION_DAYS", "AUTHENTICATED_IPS",
        "TRUST_PROXY_HEADERS", "TRUSTED_PROXY_IPS", "CORS_ALLOWED_ORIGINS", "CLIENT_LOG_MAX_BYTES",
        "UI_PUBLIC_STATUS_ENDPOINT", "UI_ALLOWED_IPS", "UI_CSRF_PROTECTION",
        "AUTH_RATE_LIMIT_WINDOW_MINUTES", "AUTH_RATE_LIMIT_MAX_ATTEMPTS",
        "ENABLE_LIVE_TV", "ENABLE_TUNARR", "TUNER_M3U_URL", "TUNER_XMLTV_URL",
        "ENABLE_STASH_CHANNELS",
        "STASH_SCHEDULE_DAYS", "STASH_KEEP_DAYS", "STASH_CHANNEL_START_NUMBER",
        "ENABLE_SHORTS_CHANNEL", "SHORTS_MAX_MINUTES",
        "FFMPEG_PATH", "LIVE_TV_IDLE_TIMEOUT",
        "LIVE_TV_HLS_LIST_SIZE", "LIVE_TV_SEG_RETENTION",
        "ENABLE_HANDY_SYNC", "HANDY_SYNC_MODE", "HANDY_APPLICATION_ID",
        "HANDY_HSP_BUFFER_MIN_S", "HANDY_HSP_BUFFER_MAX_S", "HANDY_HSP_POLL_INTERVAL_S",
    ]

    try:
        tmp_file = CONFIG_FILE + ".tmp"
        with open(tmp_file, 'w') as f:
            f.write("# Stash-Jellyfin Proxy Configuration\n")
            for key in keys_to_save:
                val = getattr(sys.modules[__name__], key, "")
                if isinstance(val, bool): val_str = str(val).lower()
                elif isinstance(val, (list, set)): val_str = ", ".join(map(str, val))
                else: val_str = str(val).strip()
                f.write(f"{key} = {val_str}\n")
        
        # Atomic replace prevents corruption if process crashes mid-write
        os.replace(tmp_file, CONFIG_FILE)
    except Exception as e:
        logger.error(f"Failed to save config: {e}")

def _coerce_config_value(key, val):
    """Responsibility: Cast raw string values to their correct Python types."""
    if key in ["CACHE_VERSION", "PROXY_PORT", "UI_PORT", "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE",
                "STASH_TIMEOUT", "STASH_RETRIES", "LOG_MAX_SIZE_MB",
                "LOG_BACKUP_COUNT", "BAN_THRESHOLD", "BAN_WINDOW_MINUTES", "RECENT_DAYS",
                "AUTH_IP_TIMEOUT_MINUTES", "TOP_PLAYED_RETENTION_DAYS",
                "AUTH_RATE_LIMIT_WINDOW_MINUTES", "AUTH_RATE_LIMIT_MAX_ATTEMPTS",
                "STASH_SCHEDULE_DAYS", "STASH_KEEP_DAYS", "STASH_CHANNEL_START_NUMBER",
                "SHORTS_MAX_MINUTES", "LIVE_TV_IDLE_TIMEOUT",
                "LIVE_TV_HLS_LIST_SIZE", "LIVE_TV_SEG_RETENTION",
                "HANDY_HSP_BUFFER_MIN_S", "HANDY_HSP_BUFFER_MAX_S", "HANDY_HSP_POLL_INTERVAL_S"]:
        try: return int(val)
        except ValueError: return None
    elif key in ["ENABLE_FILTERS", "ENABLE_TAG_FILTERS", "ENABLE_ALL_TAGS", "REQUIRE_AUTH_FOR_CONFIG",
                 "STASH_VERIFY_TLS", "TRUST_PROXY_HEADERS", "UI_PUBLIC_STATUS_ENDPOINT",
                 "UI_CSRF_PROTECTION", "ENABLE_LIVE_TV", "ENABLE_TUNARR", "ENABLE_STASH_CHANNELS",
                 "ENABLE_SHORTS_CHANNEL", "ENABLE_HANDY_SYNC"]:
        return str(val).lower() in ['true', '1', 'yes', 'on']
    elif key in ["TAG_GROUPS", "LATEST_GROUPS", "TRUSTED_PROXY_IPS", "CORS_ALLOWED_ORIGINS", "UI_ALLOWED_IPS"]:
        return [x.strip() for x in str(val).split(",") if x.strip()]
    elif key in ["BANNED_IPS"]:
        return set(x.strip() for x in str(val).split(",") if x.strip())
    elif key == "LOG_LEVEL": return str(val).upper()
    elif key == "STASH_GRAPHQL_PATH": return normalize_path(val)
    elif key == "HANDY_SYNC_MODE":
        v = str(val).strip().lower()
        return v if v in ("auto", "hosted", "local") else "auto"
    return val

# --- 3. ROBUST LOAD FUNCTION ---
def load_config_file():
    if not os.path.exists(CONFIG_FILE): return
    try:
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line: continue
                
                k, v = line.split('=', 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                
                coerced_val = _coerce_config_value(k, v)
                if coerced_val is not None:
                    setattr(sys.modules[__name__], k, coerced_val)
                    if hasattr(sys.modules[__name__], "config_defined_keys"):
                        getattr(sys.modules[__name__], "config_defined_keys").add(k)
    except Exception as e:
        logger.error(f"Config load error: {e}")

load_config_file()

# --- 4. ENVIRONMENT VARIABLES OVERRIDE ---
_supported_keys = [
    "STASH_URL", "STASH_API_KEY", "PROXY_BIND", "PROXY_PORT", "UI_PORT", "HOST_IP", "PROXY_API_KEY",
    "SJS_USER", "SJS_PASSWORD", "SERVER_ID", "SERVER_NAME", "TAG_GROUPS", "LATEST_GROUPS",
    "STASH_TIMEOUT", "STASH_RETRIES", "STASH_GRAPHQL_PATH", "STASH_VERIFY_TLS",
    "SYNC_LEVEL", "ENABLE_FILTERS", "ENABLE_TAG_FILTERS", 
    "ENABLE_ALL_TAGS", "CACHE_VERSION", "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE",
    "REQUIRE_AUTH_FOR_CONFIG", "LOG_DIR", "LOG_FILE",
    "LOG_LEVEL", "LOG_MAX_SIZE_MB", "LOG_BACKUP_COUNT", "BAN_THRESHOLD", 
    "BAN_WINDOW_MINUTES", "BANNED_IPS", "RECENT_DAYS", "FAVORITE_ACTION", "ALLOW_CLIENT_DELETION",
    "AUTH_IP_TIMEOUT_MINUTES", "TOP_PLAYED_RETENTION_DAYS", "AUTHENTICATED_IPS",
    "TRUST_PROXY_HEADERS", "TRUSTED_PROXY_IPS", "CORS_ALLOWED_ORIGINS", "CLIENT_LOG_MAX_BYTES",
    "UI_PUBLIC_STATUS_ENDPOINT", "UI_ALLOWED_IPS", "UI_CSRF_PROTECTION",
    "AUTH_RATE_LIMIT_WINDOW_MINUTES", "AUTH_RATE_LIMIT_MAX_ATTEMPTS",
    "ENABLE_LIVE_TV", "ENABLE_TUNARR", "TUNER_M3U_URL", "TUNER_XMLTV_URL",
    "ENABLE_STASH_CHANNELS",
    "STASH_SCHEDULE_DAYS", "STASH_KEEP_DAYS", "STASH_CHANNEL_START_NUMBER",
    "ENABLE_SHORTS_CHANNEL", "SHORTS_MAX_MINUTES",
    "FFMPEG_PATH", "LIVE_TV_IDLE_TIMEOUT",
    "LIVE_TV_HLS_LIST_SIZE", "LIVE_TV_SEG_RETENTION",
    "ENABLE_HANDY_SYNC", "HANDY_SYNC_MODE", "HANDY_APPLICATION_ID",
    "HANDY_HSP_BUFFER_MIN_S", "HANDY_HSP_BUFFER_MAX_S", "HANDY_HSP_POLL_INTERVAL_S",
]

for k in _supported_keys:
    val = os.getenv(k)
    if val is not None:
        coerced_val = _coerce_config_value(k, val)
        if coerced_val is not None:
            globals()[k] = coerced_val
            env_overrides.append(k)

# --- 5. AUTO-GENERATE MISSING KEYS ---
needs_save = False
if not globals().get("SERVER_ID"):
    globals()["SERVER_ID"] = uuid.uuid4().hex
    needs_save = True

if not globals().get("PROXY_API_KEY"):
    globals()["PROXY_API_KEY"] = str(uuid.uuid4())
    needs_save = True

if needs_save: save_config()