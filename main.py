import os
import sys
import time
import asyncio
import logging
import socket
import subprocess
from contextlib import asynccontextmanager
from hypercorn.config import Config
from hypercorn.asyncio import serve
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.middleware.cors import CORSMiddleware
from logging.handlers import RotatingFileHandler

from routes import routes

class _RobustRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that survives rotation failures and external file moves.

    Two failure modes are handled:

    1. Rotation failure — on some Docker bind mounts / FUSE filesystems
       os.rename() raises an exception.  The base class swallows it but leaves
       self.stream = None, so every subsequent write is silently dropped.
       We catch the failure and reopen the original file so logging continues.

    2. File moved externally — Unraid's Mover copies the log file to the array
       then deletes the cache copy.  Python's fd becomes orphaned (writing to a
       deleted inode that will never appear in the directory).  We detect this
       every _WATCH_INTERVAL seconds by comparing the inode/dev of our open fd
       against the file currently at the log path, and reopen when they diverge.
    """

    _WATCH_INTERVAL = 10.0  # seconds between orphan-detection checks

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_watch: float = 0.0

    def _reopen_if_orphaned(self) -> None:
        now = time.time()
        if now - self._last_watch < self._WATCH_INTERVAL:
            return
        self._last_watch = now
        if self.stream is None:
            return
        try:
            fd_stat   = os.fstat(self.stream.fileno())
            path_stat = os.stat(self.baseFilename)
            if fd_stat.st_ino != path_stat.st_ino or fd_stat.st_dev != path_stat.st_dev:
                self.stream.flush()
                self.stream.close()
                self.stream = self._open()
        except FileNotFoundError:
            # Path no longer exists — close orphan and open fresh
            try:
                self.stream.close()
            except Exception:
                pass
            self.stream = self._open()
        except Exception:
            pass

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except Exception:
            if self.stream is None:
                try:
                    self.stream = self._open()
                except Exception:
                    pass

    def emit(self, record) -> None:
        self._reopen_if_orphaned()
        super().emit(record)
import mimetypes

mimetypes.add_type('application/javascript', '.js')
mimetypes.add_type('text/css', '.css')

import config
import state
from core import stash_client
from core.udp_discovery import JellyfinDiscoveryProtocol
from api.middleware import AuthenticationMiddleware
from api import stream_routes, image_routes, ui_routes, userdata_routes
from api import live_tv_data as _live_tv_data

if not os.path.exists(config.LOG_DIR):
    try: os.makedirs(config.LOG_DIR, exist_ok=True)
    except Exception: config.LOG_DIR = "."

# --- BEGIN CUSTOM LOGGING INJECTION ---
NOTICE_LEVEL_NUM = 15
TRACE_LEVEL_NUM = 5

logging.addLevelName(NOTICE_LEVEL_NUM, "NOTICE")
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")

def notice(self, message, *args, **kws):
    if self.isEnabledFor(NOTICE_LEVEL_NUM):
        self._log(NOTICE_LEVEL_NUM, message, args, **kws)

def trace(self, message, *args, **kws):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)

logging.Logger.notice = notice
logging.Logger.trace = trace

class _SuppressLibraryDebugFilter(logging.Filter):
    """Drop DEBUG/INFO records from noisy third-party libraries.

    setLevel() on named loggers is the right tool, but hypercorn's serve() calls
    logging.config.dictConfig() during startup which resets every named logger's
    level back to NOTSET — nuking any setLevel() we applied at module load time.
    Attaching a filter to the handlers instead survives that reset because filters
    live on the handler object, not on logger objects.
    """
    _SUPPRESS_PREFIXES = ("httpcore", "httpx", "hpack", "h11", "h2")

    def filter(self, record: logging.LogRecord) -> bool:
        if any(record.name.startswith(p) for p in self._SUPPRESS_PREFIXES):
            return record.levelno >= logging.WARNING
        return True


logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        _RobustRotatingFileHandler(
            os.path.join(config.LOG_DIR, config.LOG_FILE),
            maxBytes=getattr(config, "LOG_MAX_SIZE_MB", 5) * 1024 * 1024,
            backupCount=getattr(config, "LOG_BACKUP_COUNT", 2),
            encoding="utf-8"
        )
    ]
)

# Belt-and-suspenders: suppress library debug at the handler level (survives
# hypercorn's dictConfig reset) and also at the logger level for the common case.
_lib_filter = _SuppressLibraryDebugFilter()
for _h in logging.root.handlers:
    _h.addFilter(_lib_filter)

logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("proxy_main")

def log_security_posture():
    ui_allow_ips = getattr(config, "UI_ALLOWED_IPS", [])
    cors_origins = getattr(config, "CORS_ALLOWED_ORIGINS", [])
    trusted_proxies = getattr(config, "TRUSTED_PROXY_IPS", [])
    logger.info("-" * 50)
    logger.info("Security Posture")
    logger.info(f"Require UI Auth: {bool(getattr(config, 'REQUIRE_AUTH_FOR_CONFIG', True))}")
    logger.info(f"Public /api/status: {bool(getattr(config, 'UI_PUBLIC_STATUS_ENDPOINT', False))}")
    logger.info(f"CSRF Protection: {bool(getattr(config, 'UI_CSRF_PROTECTION', True))}")
    logger.info(f"UI IP Allowlist Enabled: {bool(ui_allow_ips)}")
    if ui_allow_ips:
        logger.info(f"UI Allowed IPs: {', '.join(ui_allow_ips)}")
    logger.info(
        f"Auth Rate Limit: {getattr(config, 'AUTH_RATE_LIMIT_MAX_ATTEMPTS', 10)} attempts / "
        f"{getattr(config, 'AUTH_RATE_LIMIT_WINDOW_MINUTES', 15)} min"
    )
    logger.info(f"Trust Proxy Headers: {bool(getattr(config, 'TRUST_PROXY_HEADERS', False))}")
    if trusted_proxies:
        logger.info(f"Trusted Proxy IPs: {', '.join(trusted_proxies)}")
    logger.info(f"CORS Allowlist Configured: {bool(cors_origins)}")
    if cors_origins:
        logger.info(f"CORS Allowed Origins: {', '.join(cors_origins)}")
    logger.info("-" * 50)

def _get_local_ip():
    local_ip = getattr(config, "HOST_IP", "").strip()
    if not local_ip:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = getattr(config, "PROXY_BIND", "127.0.0.1")
            if local_ip == "0.0.0.0": local_ip = "127.0.0.1"
    return local_ip

CACHED_LOCAL_IP = _get_local_ip()


@asynccontextmanager
async def lifespan(app):
    _live_tv_data._load_channels_config()
    if getattr(config, "ENABLE_STASH_CHANNELS", False):
        _live_tv_data._load_schedule()
        await _live_tv_data.start_maintenance_task()
    yield
    await _live_tv_data.stop_maintenance_task()
    logger.info("Shutting down global HTTP connection pools...")
    await stash_client._manager.client.aclose()
    await stream_routes.stream_client.aclose()
    await image_routes.image_client.aclose()
    await _live_tv_data._live_client.aclose()

app = Starlette(debug=(config.LOG_LEVEL == "DEBUG"), routes=routes, lifespan=lifespan)

cors_origins = getattr(config, "CORS_ALLOWED_ORIGINS", [])
if not cors_origins:
    ui_port = getattr(config, "UI_PORT", 8097)
    cors_origins = [f"http://127.0.0.1:{ui_port}", f"http://localhost:{ui_port}"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

asgi_app = AuthenticationMiddleware(app)

async def background_pruner():
    while True:
        await asyncio.sleep(60)
        try:
            now = time.time()
            
            timeout = getattr(config, "AUTH_IP_TIMEOUT_MINUTES", 60)
            if timeout > 0 and hasattr(state, "authenticated_ips") and isinstance(state.authenticated_ips, dict):
                expired_ips = [ip for ip, ts in state.authenticated_ips.items() if now - ts > (timeout * 60)]
                if expired_ips:
                    for ip in expired_ips: del state.authenticated_ips[ip]
                    logger.trace(f"Pruned {len(expired_ips)} expired IP authentications.")
                    if hasattr(state, "save_auth_ips"): state.save_auth_ips(state.authenticated_ips)

            retention = getattr(config, "TOP_PLAYED_RETENTION_DAYS", 0)
            if retention > 0 and "top_played" in state.stats:
                expired_scenes = [sid for sid, data in state.stats["top_played"].items() if now - data.get("last_played", now) > (retention * 86400)]
                if expired_scenes:
                    for sid in expired_scenes: del state.stats["top_played"][sid]
                    logger.trace(f"Pruned {len(expired_scenes)} expired top played records.")
                    if hasattr(state, "save_stats"): state.save_stats()
                    
        except Exception as e:
            logger.error(f"Background pruner encountered an error: {e}")

async def continuous_preheater():
    import json
    from core import stash_client
    logger.debug("Starting continuous cache pre-heater for primary libraries (5-minute interval).")
    
    # We must formulate the EXACT dictionaries that library_routes generates to match the cache keys
    filter_str = json.dumps({"direction": "ASC", "sort": "title"}, sort_keys=True)
    scene_filter_all = json.dumps({}, sort_keys=True)
    scene_filter_org = json.dumps({"organized": True}, sort_keys=True)
    scene_filter_tag = json.dumps({"tags": {"modifier": "NOT_NULL"}}, sort_keys=True)
    
    while True:
        try:
            # Gather fires all 3 requests at the exact same time
            await asyncio.gather(
                stash_client.fetch_lightweight_index(filter_str, scene_filter_all),
                stash_client.fetch_lightweight_index(filter_str, scene_filter_org),
                stash_client.fetch_lightweight_index(filter_str, scene_filter_tag)
            )
            logger.trace("Primary libraries pre-heated successfully.")
        except Exception as e:
            logger.warning(f"Cache pre-heater encountered an issue: {e}")
            
        # Sleep for exactly 300 seconds (the aiocache TTL). 
        # When it wakes up, the cache will have just expired, guaranteeing a fresh pull!
        await asyncio.sleep(300)

async def run_server():
    hypercorn_config = Config()
    hypercorn_config.bind = [f"{config.PROXY_BIND}:{config.PROXY_PORT}"]
    hypercorn_config.graceful_timeout = 3.0 
    
    if hasattr(config, "UI_PORT") and config.UI_PORT != config.PROXY_PORT:
        hypercorn_config.bind.append(f"{config.PROXY_BIND}:{config.UI_PORT}")
    
    logger.info("=" * 50)
    logger.info(f"Stash-Jellyfin Proxy v2")
    logger.info(f"Proxy API: {config.PROXY_BIND}:{config.PROXY_PORT}")
    if config.PROXY_API_KEY: logger.info(f"Proxy API Key Loaded")
    logger.info("=" * 50)
    log_security_posture()

    stash_online = await stash_client.test_stash_connection()
    if not stash_online:
        logger.warning("Stash is unreachable! Proxy will start, but clients will fail to load data.")
    else:
        logger.info("Connected to Stash successfully.")
    
    loop = asyncio.get_running_loop()
    try:
        discovery_transport, _ = await loop.create_datagram_endpoint(
            lambda: JellyfinDiscoveryProtocol(CACHED_LOCAL_IP),
            local_addr=('0.0.0.0', 7359),
            allow_broadcast=True
        )
    except Exception as e:
        logger.error(f"Failed to bind UDP Discovery on port 7359: {e}")
        discovery_transport = None
        
    shutdown_trigger_event = asyncio.Event()

    async def watch_for_restart():
        while True:
            try:
                if getattr(ui_routes, "RESTART_REQUESTED", False):
                    logger.info("Restart flag detected. Initiating graceful shutdown...")
                    shutdown_trigger_event.set()
                    break
                await userdata_routes.prune_and_salvage_zombie_streams()
                state.clean_expired_quick_connects()
            except Exception as e:
                logger.error(f"Watch-for-restart encountered an error: {e}")
            await asyncio.sleep(60)

    watch_task = asyncio.create_task(watch_for_restart())
    prune_task = asyncio.create_task(background_pruner())
    preheat_task = asyncio.create_task(continuous_preheater())
    
    await serve(asgi_app, hypercorn_config, shutdown_trigger=shutdown_trigger_event.wait)
    
    watch_task.cancel()
    prune_task.cancel()
    preheat_task.cancel()
    if discovery_transport: discovery_transport.close()

def main():
    try: 
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Server stopped by user (CTRL+C).")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal server error: {e}")
        sys.exit(1)
        
    if getattr(ui_routes, "RESTART_REQUESTED", False):
        logger.info("Executing server restart...")
        time.sleep(1) 
        try: 
            logging.shutdown()
            
            script_path = os.path.abspath(__file__)
            args = [sys.executable, script_path] + sys.argv[1:]
            
            os.execv(sys.executable, args)
            
        except Exception as e:
            print(f"Failed to execute restart: {e}")
            os._exit(1)

if __name__ == "__main__":
    main()