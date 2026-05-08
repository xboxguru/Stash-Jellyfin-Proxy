import logging
import uuid
import random
import time
import os
from datetime import datetime, timezone
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.requests import Request
import config
import state

logger = logging.getLogger(__name__)

def _get_full_user() -> dict:
    """Generates a strictly-typed UserDto matched EXACTLY to the main branch."""
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

def _parse_client_info(request: Request, body_data: dict) -> dict:
    """Extracts client details from the JSON body or X-Emby-Authorization header."""
    info = {"Client": "Jellyfin-Client", "Device": "Device", "DeviceId": "proxy-v2-id", "Version": "1.0.0"}
    if body_data:
        info.update(body_data)
    
    auth_header = request.headers.get("authorization", "") or request.headers.get("x-emby-authorization", "")
    if auth_header:
        parts = auth_header.replace("MediaBrowser ", "").split(",")
        for part in parts:
            if "=" in part:
                k, v = part.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"')
                if k == "Client": info["Client"] = v
                elif k == "Device": info["Device"] = v
                elif k == "DeviceId": info["DeviceId"] = v
                elif k == "Version": info["Version"] = v
    return info

def _build_auth_payload(request: Request, fake_user: dict, request_data: dict) -> dict:
    """Builds the full auth payload, completely mirroring the main branch SessionInfo."""
    server_id = getattr(config, "SERVER_ID", "stash-proxy-server-id")
    access_token = getattr(config, "PROXY_API_KEY", "")
    client_info = _parse_client_info(request, request_data)

    return {
        "User": fake_user,
        "SessionInfo": {
            "PlayState": {
                "CanSeek": False,
                "IsPaused": False,
                "IsMuted": False,
                "RepeatMode": "RepeatNone",
                "PlaybackOrder": "Default"
            },
            "AdditionalUsers": [],
            "Capabilities": {
                "PlayableMediaTypes": [],
                "SupportedCommands": [],
                "SupportsMediaControl": False,
                "SupportsPersistentIdentifier": True
            },
            "RemoteEndPoint": request.client.host if request.client else "127.0.0.1",
            "PlayableMediaTypes": [],
            "Id": "00000000000000000000000000000002",
            "UserId": fake_user["Id"],
            "UserName": fake_user["Name"],
            "Client": client_info.get("Client"),
            "LastActivityDate": "2026-01-01T00:00:00.0000000Z",
            "LastPlaybackCheckIn": "0001-01-01T00:00:00.0000000Z",
            "DeviceName": client_info.get("Device"),
            "DeviceId": client_info.get("DeviceId"),
            "ApplicationVersion": client_info.get("Version"),
            "IsActive": True,
            "SupportsMediaControl": False,
            "SupportsRemoteControl": False,
            "NowPlayingQueue": [],
            "NowPlayingQueueFullItems": [],
            "HasCustomDeviceName": False,
            "ServerId": server_id,
            "SupportedCommands": []
        },
        "AccessToken": str(access_token),
        "ServerId": str(server_id)
    }


# --- Standard Auth Endpoints ---

async def endpoint_public_users(request: Request):
    valid_jellyfin_id = "00000000-0000-0000-0000-000000000001"
    expected_user = str(getattr(config, "SJS_USER", "admin")).strip() or "admin"
    has_pass = bool(str(getattr(config, "SJS_PASSWORD", "")).strip())
    
    return JSONResponse([{
        "Name": expected_user,
        "Id": valid_jellyfin_id,
        "ServerId": getattr(config, "SERVER_ID", ""),
        "HasPassword": has_pass,
        "HasConfiguredPassword": has_pass,
        "HasConfiguredEasyPassword": False,
        "PrimaryImageTag": None
    }])

async def endpoint_user(request: Request):
    return JSONResponse(_get_full_user())

async def endpoint_users(request: Request):
    return JSONResponse([_get_full_user()])

async def endpoint_authenticate_by_name(request: Request):
    try: data = await request.json()
    except Exception: data = {}
        
    username = data.get("Username", data.get("username", ""))
    password = data.get("Pw", data.get("pw", data.get("Password", "")))
    
    expected_user = str(getattr(config, "SJS_USER", "")).strip()
    expected_pass = str(getattr(config, "SJS_PASSWORD", "")).strip()
    client_ip = request.client.host if request.client else "127.0.0.1"
    
    if expected_user and (username.lower() != expected_user.lower() or password != expected_pass):
        logger.warning(f"Failed authentication attempt for user '{username}' from IP {client_ip}")
        return JSONResponse({"error": "Invalid username or password"}, status_code=401)
            
    logger.info(f"User '{username}' successfully authenticated from {client_ip}")
    return JSONResponse(_build_auth_payload(request, _get_full_user(), data))

# --- System Endpoints ---

async def endpoint_system_info_public(request: Request):
    host = request.headers.get("host", "192.168.0.21:8096") 
    return JSONResponse({
        "LocalAddress": f"http://{host}",
        "ServerName": getattr(config, "SERVER_NAME", "Stash Proxy"),
        "Version": "10.11.6", "ProductName": "Jellyfin Server", "OperatingSystem": "Linux",
        "Id": getattr(config, "SERVER_ID", "stash-proxy-unique-id"), "StartupWizardCompleted": True   
    })

async def endpoint_system_info(request: Request):
    host = request.headers.get("host", f"127.0.0.1:{getattr(config, 'PROXY_PORT', 8096)}")
    return JSONResponse({
        "LocalAddress": f"{request.url.scheme}://{host}",
        "ServerName": getattr(config, "SERVER_NAME", "Stash Proxy") or "Stash Proxy",
        "Version": "10.11.6", "ProductName": "Jellyfin Server", "OperatingSystem": "Linux",
        "Id": getattr(config, "SERVER_ID", "stash-proxy-server-id-01"), "StartupWizardCompleted": True   
    })

async def endpoint_system_ping(request: Request):
    return PlainTextResponse("Jellyfin Server")

async def endpoint_branding_configuration(request: Request):
    return JSONResponse({})

async def endpoint_client_log(request: Request):
    """Intercepts client application logs and saves them to disk for debugging."""
    try:
        # 1. Grab the raw log text sent by the app
        body = await request.body()
        client_ip = request.client.host if request.client else "unknown"
        
        # 2. Extract the actual Client Name using our existing helper
        client_info = _parse_client_info(request, {})
        client_name = client_info.get("Client", "UnknownClient")
        
        # Sanitize the client name so it's safe for Windows/Linux filesystems
        safe_client_name = "".join(c if c.isalnum() else "_" for c in client_name).strip("_")
        
        # 3. Ensure the client_logs directory exists
        log_dir = os.path.join(getattr(config, "LOG_DIR", "."), "client_logs")
        os.makedirs(log_dir, exist_ok=True)
        
        # 4. Save with format: Jellyfin_AndroidTV_192.168.0.173_1775085060.log
        filename = os.path.join(log_dir, f"{safe_client_name}_{client_ip}_{int(time.time())}.log")
        
        with open(filename, "wb") as f:
            f.write(body)
            
        logger.info(f"📥 Client crash log successfully caught and saved to {filename}")
        
    except Exception as e:
        logger.error(f"Failed to save client log: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
        
    # Always return OK so the client knows it was received
    return JSONResponse({"FileName": os.path.basename(filename)})

# --- Quick Connect Endpoints ---
async def endpoint_quickconnect_enabled(request: Request):
    return Response(content="true", media_type="application/json")

async def endpoint_quickconnect_initiate(request: Request):
    state.clean_expired_quick_connects() 
    secret = str(uuid.uuid4())
    code = str(random.randint(100000, 999999))
    timestamp = time.time()
    
    state.quick_connect_sessions[secret] = {
        "code": code, 
        "authorized": False, 
        "timestamp": timestamp
    }
    logger.info(f"QuickConnect initiated. Code: {code} | Secret: {secret}")
    
    # Parse the client info to populate the required fields
    client_info = _parse_client_info(request, {})
    
    # Return the full QuickConnectResult object instead of just Secret/Code
    response_data = {
        "Authenticated": False,
        "Secret": secret,
        "Code": code,
        "DeviceId": client_info.get("DeviceId", "proxy-handshake-device"),
        "DeviceName": client_info.get("Device", "Stash Proxy Handshake"),
        "AppName": client_info.get("Client", "Stash-Jellyfin-Proxy"),
        "AppVersion": client_info.get("Version", "1.0.0"),
        "DateAdded": datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    }
    
    return JSONResponse(response_data)

async def endpoint_quickconnect_connect(request: Request):
    secret = request.query_params.get("secret")
    session = state.quick_connect_sessions.get(secret)
    
    if not session:
        return Response(status_code=404)
        
    client_info = _parse_client_info(request, {})
    
    response_data = {
        "Authenticated": session.get("authorized", False),
        "Secret": secret,
        "Code": session["code"],
        "DeviceId": client_info.get("DeviceId", "proxy-handshake-device"),
        "DeviceName": client_info.get("Device", "Stash Proxy Handshake"),
        "AppName": client_info.get("Client", "Stash-Jellyfin-Proxy"),
        "AppVersion": client_info.get("Version", "1.0.0"),
        "DateAdded": datetime.fromtimestamp(session["timestamp"], tz=timezone.utc).isoformat().replace("+00:00", "Z")
    }
    return JSONResponse(response_data)

async def endpoint_quickconnect_authorize(request: Request):
    code = request.query_params.get("code")
    for secret, session in state.quick_connect_sessions.items():
        if session["code"] == code:
            session["authorized"] = True
            logger.info(f"QuickConnect Web UI approved code: {code}")
            return JSONResponse({"Success": True})
            
    return JSONResponse({"Error": "Invalid code"}, status_code=400)

async def endpoint_authenticate_by_quickconnect(request: Request):
    """Finalizes QuickConnect by swapping Secret for Token."""
    try:
        data = await request.json()
        secret = data.get("Secret")
    except Exception:
        return Response(status_code=400)

    session = state.quick_connect_sessions.get(secret)
    if not session or not session.get("authorized"):
        return Response(status_code=401)

    logger.info(f"Finalizing QuickConnect login for secret: {secret}")
    
    user_data = _get_full_user()
    response_payload = _build_auth_payload(request, user_data, data)
    
    del state.quick_connect_sessions[secret]
    return JSONResponse(response_payload)

async def endpoint_blackhole(request: Request):
    path_lower = request.url.path.lower()
    
    if "syncplay" in path_lower: return JSONResponse([])
    if "sessions" in path_lower and request.method == "GET": return JSONResponse([])
    if "branding/css" in path_lower: return Response(content="", media_type="text/css")

    array_endpoints = ["/plugins", "/scheduledtasks", "/channels", "/livetv", "/providers"]
    if any(x in path_lower for x in array_endpoints): return JSONResponse([])
        
    if "configuration" in path_lower:
        return JSONResponse({"PlayDefaultAudioTrack": True, "SubtitleMode": "Default"})

    return JSONResponse({})