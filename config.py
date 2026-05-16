import os
import sys
import uuid
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
                "SHORTS_MAX_MINUTES", "LIVE_TV_IDLE_TIMEOUT"]:
        try: return int(val)
        except ValueError: return None
    elif key in ["ENABLE_FILTERS", "ENABLE_TAG_FILTERS", "ENABLE_ALL_TAGS", "REQUIRE_AUTH_FOR_CONFIG",
                 "STASH_VERIFY_TLS", "TRUST_PROXY_HEADERS", "UI_PUBLIC_STATUS_ENDPOINT",
                 "UI_CSRF_PROTECTION", "ENABLE_LIVE_TV", "ENABLE_TUNARR", "ENABLE_STASH_CHANNELS",
                 "ENABLE_SHORTS_CHANNEL"]:
        return str(val).lower() in ['true', '1', 'yes', 'on']
    elif key in ["TAG_GROUPS", "LATEST_GROUPS", "TRUSTED_PROXY_IPS", "CORS_ALLOWED_ORIGINS", "UI_ALLOWED_IPS"]:
        return [x.strip() for x in str(val).split(",") if x.strip()]
    elif key in ["BANNED_IPS"]:
        return set(x.strip() for x in str(val).split(",") if x.strip())
    elif key == "LOG_LEVEL": return str(val).upper()
    elif key == "STASH_GRAPHQL_PATH": return normalize_path(val)
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