import logging
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.requests import Request
import config
import os
import time

logger = logging.getLogger(__name__)

def _get_full_user():
    valid_jellyfin_id = "00000000-0000-0000-0000-000000000001"
    server_id = getattr(config, "SERVER_ID", "stash-proxy-server-id")
    expected_user = str(getattr(config, "SJS_USER", "admin")).strip() or "admin"
    expected_pass = str(getattr(config, "SJS_PASSWORD", "")).strip()
    has_pass = bool(expected_pass)

    return {
        "Name": expected_user,
        "ServerId": server_id,
        "Id": valid_jellyfin_id,
        "HasPassword": has_pass,
        "HasConfiguredPassword": has_pass,
        "HasConfiguredEasyPassword": False,
        "EnableAutoLogin": False,
        "LastLoginDate": "2026-01-01T00:00:00.0000000Z",
        "LastActivityDate": "2026-01-01T00:00:00.0000000Z",
        "Configuration": {
            "AudioLanguagePreference": "",
            "PlayDefaultAudioTrack": True,
            "SubtitleLanguagePreference": "",
            "DisplayMissingEpisodes": True,
            "GroupedFolders": [],
            "SubtitleMode": "Default",
            "DisplayCollectionsView": False,
            "EnableLocalPassword": False,
            "OrderedViews": [],
            "LatestItemsExcludes": [],
            "MyMediaExcludes": [],  
            "HidePlayedInLatest": True,
            "RememberAudioSelections": True,
            "RememberSubtitleSelections": True,
            "EnableNextEpisodeAutoPlay": True,
            "CastReceiverId": "F007D354" 
        },
        "Policy": {
            "IsAdministrator": True,
            "IsHidden": False,
            "EnableCollectionManagement": False, 
            "EnableSubtitleManagement": False,   
            "EnableLyricManagement": False,      
            "IsDisabled": False,
            "BlockedTags": [],
            "AllowedTags": [],                   
            "EnableUserPreferenceAccess": True,
            "AccessSchedules": [],
            "BlockUnratedItems": [],
            "EnableRemoteControlOfOtherUsers": True,
            "EnableSharedDeviceControl": True,
            "EnableRemoteAccess": True,
            "EnableLiveTvManagement": False,
            "EnableLiveTvAccess": False,
            "EnableMediaPlayback": True,
            "EnableAudioPlaybackTranscoding": True,
            "EnableVideoPlaybackTranscoding": True,
            "EnablePlaybackRemuxing": True,
            "ForceRemoteSourceTranscoding": False,
            "EnableContentDeletion": False,
            "EnableContentDeletionFromFolders": [],
            "EnableContentDownloading": True,
            "EnableSyncTranscoding": False,
            "EnableMediaConversion": False,
            "EnabledDevices": [],
            "EnableAllDevices": True,
            "EnabledChannels": [],
            "EnableAllChannels": True,
            "EnabledFolders": [],
            "EnableAllFolders": True,
            "InvalidLoginAttemptCount": 0,
            "LoginAttemptsBeforeLockout": -1, 
            "MaxActiveSessions": 0,
            "EnablePublicSharing": False,
            "BlockedMediaFolders": [],
            "BlockedChannels": [],
            "RemoteClientBitrateLimit": 0,
            "AuthenticationProviderId": "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider",
            "PasswordResetProviderId": "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider",
            "SyncPlayAccess": "CreateAndJoinGroups"
        }
    }

def _build_session_info(fake_user: dict, request_data: dict, server_id: str, client_ip: str) -> dict:
    return {
        "PlayState": {
            "CanSeek": False, "IsPaused": False, "IsMuted": False,
            "RepeatMode": "RepeatNone", "PlaybackOrder": "Default"
        },
        "AdditionalUsers": [],
        "Capabilities": {
            "PlayableMediaTypes": [], "SupportedCommands": [],
            "SupportsMediaControl": False, "SupportsPersistentIdentifier": True
        },
        "RemoteEndPoint": client_ip,
        "PlayableMediaTypes": [],
        "Id": "00000000000000000000000000000002",
        "UserId": fake_user["Id"],
        "UserName": fake_user["Name"],
        "Client": request_data.get("Client", "Findroid"),
        "LastActivityDate": "2026-01-01T00:00:00.0000000Z",
        "LastPlaybackCheckIn": "0001-01-01T00:00:00.0000000Z",
        "DeviceName": request_data.get("Device", "Device"),
        "DeviceId": request_data.get("DeviceId", "12345"),
        "ApplicationVersion": request_data.get("Version", "1.0.0"),
        "IsActive": True,
        "SupportsMediaControl": False,
        "SupportsRemoteControl": False,
        "NowPlayingQueue": [],
        "NowPlayingQueueFullItems": [],
        "HasCustomDeviceName": False,
        "ServerId": server_id,
        "SupportedCommands": []
    }

async def endpoint_public_users(request: Request):
    logger.debug("Generating public users list for login screen.")
    valid_jellyfin_id = "00000000-0000-0000-0000-000000000001"
    expected_user = str(getattr(config, "SJS_USER", "admin")).strip() or "admin"
    has_pass = bool(str(getattr(config, "SJS_PASSWORD", "")).strip())
    
    public_user = {
        "Name": expected_user,
        "Id": valid_jellyfin_id,
        "ServerId": getattr(config, "SERVER_ID", ""),
        "HasPassword": has_pass,
        "HasConfiguredPassword": has_pass,
        "HasConfiguredEasyPassword": False,
        "PrimaryImageTag": None
    }
    return JSONResponse([public_user])

async def endpoint_user(request: Request):
    logger.debug(f"Client requested detailed user configuration profile.")
    return JSONResponse(_get_full_user())

async def endpoint_users(request: Request):
    return JSONResponse([_get_full_user()])

async def endpoint_authenticate_by_name(request: Request):
    try: data = await request.json()
    except Exception: data = {}
        
    username = data.get("Username") or data.get("username") or ""
    password = data.get("Pw") or data.get("pw") or data.get("Password") or ""
    
    expected_user = str(getattr(config, "SJS_USER", "")).strip()
    expected_pass = str(getattr(config, "SJS_PASSWORD", "")).strip()
    client_ip = request.client.host if request.client else "127.0.0.1"
    
    if expected_user:
        if username.lower() != expected_user.lower() or password != expected_pass:
            logger.warning(f"Failed Jellyfin Client login attempt for user '{username}' from IP {client_ip}")
            return JSONResponse({"error": "Invalid username or password"}, status_code=401)
            
    logger.info(f"User '{username}' successfully authenticated from {client_ip}")
    fake_user = _get_full_user()
    server_id = getattr(config, "SERVER_ID", "stash-proxy-server-id")
    
    return JSONResponse({
        "User": fake_user,
        "SessionInfo": _build_session_info(fake_user, data, server_id, client_ip),
        "AccessToken": getattr(config, "PROXY_API_KEY", ""),
        "ServerId": server_id
    })

async def endpoint_system_info_public(request: Request):
    host = request.headers.get("host", "192.168.0.21:8096") 
    return JSONResponse({
        "LocalAddress": f"http://{host}",
        "ServerName": getattr(config, "SERVER_NAME", "Stash Proxy"),
        "Version": "10.11.6",           
        "ProductName": "Jellyfin Server", 
        "OperatingSystem": "Linux",
        "Id": getattr(config, "SERVER_ID", "stash-proxy-unique-id"),
        "StartupWizardCompleted": True   
    })

async def endpoint_system_info(request: Request):
    host = request.headers.get("host", f"127.0.0.1:{getattr(config, 'PROXY_PORT', 8096)}")
    scheme = request.url.scheme
    return JSONResponse({
        "LocalAddress": f"{scheme}://{host}",
        "ServerName": getattr(config, "SERVER_NAME", "Stash Proxy") or "Stash Proxy",
        "Version": "10.11.6",
        "ProductName": "Jellyfin Server",
        "OperatingSystem": "Linux",
        "Id": getattr(config, "SERVER_ID", "") or "stash-proxy-server-id-01",
        "StartupWizardCompleted": True   
    })

async def endpoint_quickconnect_enabled(request: Request):
    return JSONResponse(False)

async def endpoint_quickconnect_initiate(request: Request):
    return JSONResponse({"error": "QuickConnect is not supported on this proxy."}, status_code=400)

async def endpoint_system_ping(request: Request):
    return PlainTextResponse("Jellyfin Server")

async def endpoint_branding_configuration(request: Request):
    return JSONResponse({})

async def endpoint_client_log(request: Request):
    try:
        body = await request.body()
        client_ip = request.client.host if request.client else "unknown"
        log_dir = os.path.join(getattr(config, "LOG_DIR", "."), "client_logs")
        os.makedirs(log_dir, exist_ok=True)
        filename = os.path.join(log_dir, f"client_{client_ip}_{int(time.time())}.log")
        
        with open(filename, "wb") as f:
            f.write(body)
        logger.debug(f"Intercepted and saved client debug log to {filename}")
    except Exception as e:
        logger.error(f"Failed to save client log: {e}")
    return PlainTextResponse("OK")

async def endpoint_blackhole(request: Request):
    path_lower = request.url.path.lower()
    logger.debug(f"Unhandled Web UI Path Intercepted: {request.method} {request.url.path}")
    
    if "syncplay" in path_lower: return JSONResponse([])
    if "sessions" in path_lower and request.method == "GET": return JSONResponse([])
    if "branding/css" in path_lower: return Response(content="", media_type="text/css")

    array_endpoints = ["/plugins", "/scheduledtasks", "/channels", "/livetv", "/providers"]
    if any(x in path_lower for x in array_endpoints):
        return JSONResponse([])
        
    if "configuration" in path_lower:
        return JSONResponse({"PlayDefaultAudioTrack": True, "SubtitleMode": "Default"})

    return JSONResponse({})