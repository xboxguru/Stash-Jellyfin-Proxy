import os
import sys
import uuid

# Config file location
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.getenv("CONFIG_FILE", os.path.join(SCRIPT_DIR, "stash_jellyfin_proxy.conf"))

# Default Configuration
STASH_URL = "https://stash:9999"
STASH_API_KEY = ""
PROXY_API_KEY = ""  # NEW: Configurable Proxy API Key for ErsatzTV
SYNC_LEVEL = "Everything"  # Options: Everything, Organized, Tagged
PROXY_BIND = "0.0.0.0"
PROXY_PORT = 8096
UI_PORT = 8097
SJS_USER = ""
SJS_PASSWORD = ""
TAG_GROUPS = []
LATEST_GROUPS = ["Scenes"]
SERVER_NAME = "Stash Media Server"
SERVER_ID = ""
LIBRARY_TYPE = "movies"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
IMAGE_CACHE_MAX_SIZE = 100
ENABLE_FILTERS = True
ENABLE_IMAGE_RESIZE = True
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
IMAGE_VERSION = 0 # Cache busting

def load_config_file(filepath):
    """Load configuration from a shell-style config file."""
    config_dict = {}
    defined_keys_set = set()
    if os.path.isfile(filepath):
        try:
            with open(filepath, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        key, _, value = line.partition('=')
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        config_dict[key] = value
                        defined_keys_set.add(key)
        except Exception as e:
            print(f"Error loading config file {filepath}: {e}", file=sys.stderr)
    return config_dict, defined_keys_set

def parse_bool(value, default=True):
    if isinstance(value, bool): return value
    if isinstance(value, str): return value.lower() in ('true', 'yes', '1', 'on')
    return default

def normalize_path(path, default="/graphql"):
    if not path or not path.strip(): return default
    p = path.strip()
    if not p.startswith('/'): p = '/' + p
    if len(p) > 1 and p.endswith('/'): p = p.rstrip('/')
    return p

def save_setting_to_config(config_file, setting_key, setting_value):
    """Save a setting to the config file (Used for SERVER_ID and PROXY_API_KEY)."""
    if not os.path.isfile(config_file):
        with open(config_file, 'w') as f:
            f.write(f'# Auto-generated config\n{setting_key} = "{setting_value}"\n')
        return True

    with open(config_file, 'r') as f:
        lines = f.readlines()

    updated = False
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if (stripped.startswith('#') and setting_key in stripped and '=' in stripped) or \
           (stripped.startswith(setting_key) and '=' in stripped):
            new_lines.append(f'{setting_key} = "{setting_value}"\n')
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        new_lines.append(f'\n# Auto-generated\n{setting_key} = "{setting_value}"\n')

    with open(config_file, 'w') as f:
        f.writelines(new_lines)
    return True

# Load from .conf file
_config, config_defined_keys = load_config_file(CONFIG_FILE)
if _config:
    STASH_URL = _config.get("STASH_URL", STASH_URL)
    STASH_API_KEY = _config.get("STASH_API_KEY", STASH_API_KEY)
    SYNC_LEVEL = _config.get("SYNC_LEVEL", SYNC_LEVEL)
    PROXY_API_KEY = _config.get("PROXY_API_KEY", PROXY_API_KEY)
    PROXY_BIND = _config.get("PROXY_BIND", PROXY_BIND)
    PROXY_PORT = int(_config.get("PROXY_PORT", PROXY_PORT))
    UI_PORT = int(_config.get("UI_PORT", UI_PORT)) if "UI_PORT" in _config else UI_PORT
    SJS_USER = _config.get("SJS_USER", SJS_USER)
    SJS_PASSWORD = _config.get("SJS_PASSWORD", SJS_PASSWORD)
    
    if _config.get("TAG_GROUPS"): TAG_GROUPS = [t.strip() for t in _config.get("TAG_GROUPS").split(",") if t.strip()]
    if _config.get("LATEST_GROUPS"): LATEST_GROUPS = [t.strip() for t in _config.get("LATEST_GROUPS").split(",") if t.strip()]
    
    SERVER_NAME = _config.get("SERVER_NAME", SERVER_NAME)
    SERVER_ID = _config.get("SERVER_ID", SERVER_ID)
    DEFAULT_PAGE_SIZE = int(_config.get("DEFAULT_PAGE_SIZE", DEFAULT_PAGE_SIZE))
    MAX_PAGE_SIZE = int(_config.get("MAX_PAGE_SIZE", MAX_PAGE_SIZE))
    IMAGE_CACHE_MAX_SIZE = int(_config.get("IMAGE_CACHE_MAX_SIZE", IMAGE_CACHE_MAX_SIZE))
    IMAGE_VERSION = int(_config.get("IMAGE_VERSION", IMAGE_VERSION))
    
    ENABLE_FILTERS = parse_bool(_config.get("ENABLE_FILTERS"), ENABLE_FILTERS)
    ENABLE_IMAGE_RESIZE = parse_bool(_config.get("ENABLE_IMAGE_RESIZE"), ENABLE_IMAGE_RESIZE)
    ENABLE_TAG_FILTERS = parse_bool(_config.get("ENABLE_TAG_FILTERS"), ENABLE_TAG_FILTERS)
    ENABLE_ALL_TAGS = parse_bool(_config.get("ENABLE_ALL_TAGS"), ENABLE_ALL_TAGS)
    REQUIRE_AUTH_FOR_CONFIG = parse_bool(_config.get("REQUIRE_AUTH_FOR_CONFIG"), REQUIRE_AUTH_FOR_CONFIG)
    
    STASH_TIMEOUT = int(_config.get("STASH_TIMEOUT", STASH_TIMEOUT))
    STASH_RETRIES = int(_config.get("STASH_RETRIES", STASH_RETRIES))
    STASH_GRAPHQL_PATH = normalize_path(_config.get("STASH_GRAPHQL_PATH", STASH_GRAPHQL_PATH))
    STASH_VERIFY_TLS = parse_bool(_config.get("STASH_VERIFY_TLS"), STASH_VERIFY_TLS)
    
    LOG_DIR = _config.get("LOG_DIR", LOG_DIR)
    LOG_FILE = _config.get("LOG_FILE", LOG_FILE)
    LOG_LEVEL = _config.get("LOG_LEVEL", LOG_LEVEL).upper()
    LOG_MAX_SIZE_MB = int(_config.get("LOG_MAX_SIZE_MB", LOG_MAX_SIZE_MB))
    LOG_BACKUP_COUNT = int(_config.get("LOG_BACKUP_COUNT", LOG_BACKUP_COUNT))
    
    if _config.get("BANNED_IPS"): BANNED_IPS = set(ip.strip() for ip in _config.get("BANNED_IPS").split(",") if ip.strip())
    BAN_THRESHOLD = int(_config.get("BAN_THRESHOLD", BAN_THRESHOLD))
    BAN_WINDOW_MINUTES = int(_config.get("BAN_WINDOW_MINUTES", BAN_WINDOW_MINUTES))

# Environment Variables Override
env_overrides = []
env_map = {
    "STASH_URL": "STASH_URL", "STASH_API_KEY": "STASH_API_KEY", "PROXY_API_KEY": "PROXY_API_KEY",
    "PROXY_BIND": "PROXY_BIND", "SJS_USER": "SJS_USER", "SJS_PASSWORD": "SJS_PASSWORD",
    "SERVER_ID": "SERVER_ID", "LOG_DIR": "LOG_DIR", "SYNC_LEVEL": "SYNC_LEVEL"
}

for env_key, var_name in env_map.items():
    if os.getenv(env_key):
        globals()[var_name] = os.getenv(env_key)
        env_overrides.append(env_key)

if os.getenv("PROXY_PORT"): PROXY_PORT = int(os.getenv("PROXY_PORT")); env_overrides.append("PROXY_PORT")
if os.getenv("UI_PORT"): UI_PORT = int(os.getenv("UI_PORT")); env_overrides.append("UI_PORT")

# Auto-generate IDs/Keys if missing
if not SERVER_ID:
    SERVER_ID = uuid.uuid4().hex
    save_setting_to_config(CONFIG_FILE, "SERVER_ID", SERVER_ID)

if not PROXY_API_KEY:
    PROXY_API_KEY = str(uuid.uuid4())
    save_setting_to_config(CONFIG_FILE, "PROXY_API_KEY", PROXY_API_KEY)