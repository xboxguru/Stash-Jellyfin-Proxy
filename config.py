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
STASH_URL = "https://stash:9999"
STASH_API_KEY = ""
PROXY_API_KEY = ""  
SYNC_LEVEL = "Everything"  
PROXY_BIND = "0.0.0.0"
PROXY_PORT = 8096
UI_PORT = 8097
HOST_IP = ""  # NEW: For UDP discovery behind Docker networks
SJS_USER = ""
SJS_PASSWORD = ""
TAG_GROUPS = []
LATEST_GROUPS = ["Scenes"]
SERVER_NAME = "Stash Media Server"
SERVER_ID = "stash-jellyfin-proxy-v2-unique-id"
LIBRARY_TYPE = "movies"
RECENT_DAYS = 14
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
ENABLE_FILTERS = True
ENABLE_TAG_FILTERS = False
ENABLE_ALL_TAGS = False
REQUIRE_AUTH_FOR_CONFIG = False
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

config_defined_keys = set()
env_overrides = []

def normalize_path(path, default="/graphql"):
    if not path or not isinstance(path, str) or not path.strip(): return default
    p = path.strip()
    if not p.startswith('/'): p = '/' + p
    if len(p) > 1 and p.endswith('/'): p = p.rstrip('/')
    return p

# NEW: Centralized Stash Base URL generator
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
        "BAN_WINDOW_MINUTES", "BANNED_IPS", "RECENT_DAYS"
    ]
    
    try:
        with open(CONFIG_FILE, 'w') as f:
            f.write("# Stash-Jellyfin Proxy Configuration\n")
            for key in keys_to_save:
                val = getattr(sys.modules[__name__], key, "")
                
                if isinstance(val, bool):
                    val_str = str(val).lower()
                elif isinstance(val, (list, set)):
                    val_str = ", ".join(map(str, val))
                else:
                    val_str = str(val).strip()
                    
                f.write(f"{key} = {val_str}\n")
    except Exception as e:
        logger.error(f"Failed to save config: {e}")

# --- 3. ROBUST LOAD FUNCTION ---
def load_config_file():
    if not os.path.exists(CONFIG_FILE):
        return
        
    try:
        with open(CONFIG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    
                    if k in ["CACHE_VERSION", "PROXY_PORT", "UI_PORT", "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", 
                             "STASH_TIMEOUT", "STASH_RETRIES", "LOG_MAX_SIZE_MB", 
                             "LOG_BACKUP_COUNT", "BAN_THRESHOLD", "BAN_WINDOW_MINUTES", "RECENT_DAYS"]:
                        try: v = int(v)
                        except ValueError: continue
                    elif k in ["ENABLE_FILTERS", "ENABLE_TAG_FILTERS", "ENABLE_ALL_TAGS", "REQUIRE_AUTH_FOR_CONFIG", "STASH_VERIFY_TLS"]:
                        v = str(v).lower() in ['true', '1', 'yes', 'on']
                    elif k in ["TAG_GROUPS", "LATEST_GROUPS"]:
                        v = [x.strip() for x in v.split(",") if x.strip()]
                    elif k in ["BANNED_IPS"]:
                        v = set(x.strip() for x in v.split(",") if x.strip())
                    elif k == "LOG_LEVEL":
                        v = str(v).upper()
                    elif k == "STASH_GRAPHQL_PATH":
                        v = normalize_path(v)
                        
                    setattr(sys.modules[__name__], k, v)
                    
                    if hasattr(sys.modules[__name__], "config_defined_keys"):
                        getattr(sys.modules[__name__], "config_defined_keys").add(k)
                        
    except Exception as e:
        logger.error(f"Config load error: {e}")

load_config_file()

# --- 4. ENVIRONMENT VARIABLES OVERRIDE ---
# Dynamically check all supported configuration keys against the environment
_supported_keys = [
    "STASH_URL", "STASH_API_KEY", "PROXY_BIND", "PROXY_PORT", "UI_PORT", "HOST_IP", "PROXY_API_KEY",
    "SJS_USER", "SJS_PASSWORD", "SERVER_ID", "SERVER_NAME", "TAG_GROUPS", "LATEST_GROUPS",
    "STASH_TIMEOUT", "STASH_RETRIES", "STASH_GRAPHQL_PATH", "STASH_VERIFY_TLS",
    "SYNC_LEVEL", "ENABLE_FILTERS", "ENABLE_TAG_FILTERS", 
    "ENABLE_ALL_TAGS", "CACHE_VERSION", "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE",
    "REQUIRE_AUTH_FOR_CONFIG", "LOG_DIR", "LOG_FILE", 
    "LOG_LEVEL", "LOG_MAX_SIZE_MB", "LOG_BACKUP_COUNT", "BAN_THRESHOLD", 
    "BAN_WINDOW_MINUTES", "BANNED_IPS", "RECENT_DAYS"
]

for k in _supported_keys:
    val = os.getenv(k)
    if val is not None:
        if k in ["CACHE_VERSION", "PROXY_PORT", "UI_PORT", "DEFAULT_PAGE_SIZE", "MAX_PAGE_SIZE", 
                 "STASH_TIMEOUT", "STASH_RETRIES", "LOG_MAX_SIZE_MB", 
                 "LOG_BACKUP_COUNT", "BAN_THRESHOLD", "BAN_WINDOW_MINUTES", "RECENT_DAYS"]:
            try: val = int(val)
            except ValueError: continue
        elif k in ["ENABLE_FILTERS", "ENABLE_TAG_FILTERS", "ENABLE_ALL_TAGS", "REQUIRE_AUTH_FOR_CONFIG", "STASH_VERIFY_TLS"]:
            val = str(val).lower() in ['true', '1', 'yes', 'on']
        elif k in ["TAG_GROUPS", "LATEST_GROUPS"]:
            val = [x.strip() for x in val.split(",") if x.strip()]
        elif k in ["BANNED_IPS"]:
            val = set(x.strip() for x in val.split(",") if x.strip())
        elif k == "LOG_LEVEL":
            val = str(val).upper()
        elif k == "STASH_GRAPHQL_PATH":
            val = normalize_path(val)
            
        globals()[k] = val
        env_overrides.append(k)

# --- 5. AUTO-GENERATE MISSING KEYS ---
needs_save = False

if not globals().get("SERVER_ID"):
    globals()["SERVER_ID"] = uuid.uuid4().hex
    needs_save = True

if not globals().get("PROXY_API_KEY"):
    globals()["PROXY_API_KEY"] = str(uuid.uuid4())
    needs_save = True

if needs_save:
    save_config()