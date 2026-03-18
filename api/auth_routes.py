import logging
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.requests import Request
import config

logger = logging.getLogger(__name__)

def _get_full_user():
    """Generates a strictly-typed UserDto matched to a real Jellyfin Server response."""
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
            "MyMediaExcludes": [],  # Fixed from MyViewsExcludes
            "HidePlayedInLatest": True,
            "RememberAudioSelections": True,
            "RememberSubtitleSelections": True,
            "EnableNextEpisodeAutoPlay": True,
            "CastReceiverId": "F007D354" # Added from real server
        },
        "Policy": {
            "IsAdministrator": True,
            "IsHidden": False,
            "EnableCollectionManagement": False, # Added
            "EnableSubtitleManagement": False,   # Added
            "EnableLyricManagement": False,      # Added
            "IsDisabled": False,
            "BlockedTags": [],
            "AllowedTags": [],                   # Added
            "EnableUserPreferenceAccess": True,
            "AccessSchedules": [],
            "BlockUnratedItems": [],
            "EnableRemoteControlOfOtherUsers": True,
            "EnableSharedDeviceControl": True,
            "EnableRemoteAccess": True,
            "EnableLiveTvManagement": False,
            "EnableLiveTvAccess": False,
            "EnableMediaPlayback": True,
            "EnableAudioPlaybackTranscoding": False,
            "EnableVideoPlaybackTranscoding": False,
            "EnablePlaybackRemuxing": False,
            "ForceRemoteSourceTranscoding": False,
            "EnableContentDeletion": False,
            "EnableContentDeletionFromFolders": [],
            "EnableContentDownloading": False,
            "EnableSyncTranscoding": False,
            "EnableMediaConversion": False,
            "EnabledDevices": [],
            "EnableAllDevices": True,
            "EnabledChannels": [],
            "EnableAllChannels": True,
            "EnabledFolders": [],
            "EnableAllFolders": True,
            "InvalidLoginAttemptCount": 0,
            "LoginAttemptsBeforeLockout": -1, # Matched to real
            "MaxActiveSessions": 0,
            "EnablePublicSharing": False,
            "BlockedMediaFolders": [],
            "BlockedChannels": [],
            "RemoteClientBitrateLimit": 0,
            # FIXED: Changed from Emby to Jellyfin namespaces
            "AuthenticationProviderId": "Jellyfin.Server.Implementations.Users.DefaultAuthenticationProvider",
            "PasswordResetProviderId": "Jellyfin.Server.Implementations.Users.DefaultPasswordResetProvider",
            "SyncPlayAccess": "CreateAndJoinGroups"
        }
    }

async def endpoint_public_users(request: Request):
    """Findroid uses this to list users on the login screen. MUST be a simple PublicUserInfo object."""
    valid_jellyfin_id = "00000000-0000-0000-0000-000000000001"
    expected_user = str(getattr(config, "SJS_USER", "admin")).strip() or "admin"
    has_pass = bool(str(getattr(config, "SJS_PASSWORD", "")).strip())
    
    # Do NOT include Policy or Configuration here, or Kotlin parsers will crash!
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
    """Returns the user details when Jellycon verifies the login."""
    return JSONResponse(_get_full_user())

async def endpoint_users(request: Request):
    """Returns a fake user list containing our single proxy user."""
    return JSONResponse([_get_full_user()])

async def endpoint_authenticate_by_name(request: Request):
    """Authenticates the user and hands the client our Proxy API Key."""
    try:
        data = await request.json()
    except Exception:
        data = {}
        
    username = data.get("Username") or data.get("username") or ""
    password = data.get("Pw") or data.get("pw") or data.get("Password") or ""
    
    expected_user = str(getattr(config, "SJS_USER", "")).strip()
    expected_pass = str(getattr(config, "SJS_PASSWORD", "")).strip()
    
    # ENFORCE SECURITY: Check credentials
    if expected_user:
        if username.lower() != expected_user.lower() or password != expected_pass:
            logger.warning(f"Failed login attempt for user: {username}")
            return JSONResponse({"error": "Invalid username or password"}, status_code=401)
            
    fake_user = _get_full_user()
    server_id = getattr(config, "SERVER_ID", "stash-proxy-server-id")
    
    return JSONResponse({
        "User": fake_user,
        "SessionInfo": {
            "PlayState": {
                "CanSeek": False,
                "IsPaused": False,
                "IsMuted": False,
                "RepeatMode": "RepeatNone",
                "PlaybackOrder": "Default" # Added
            },
            "AdditionalUsers": [],
            "Capabilities": {
                "PlayableMediaTypes": [],
                "SupportedCommands": [],
                "SupportsMediaControl": False,
                "SupportsPersistentIdentifier": True # Matched to real
            },
            "RemoteEndPoint": request.client.host if request.client else "127.0.0.1",
            "PlayableMediaTypes": [],
            "Id": "00000000000000000000000000000002",
            "UserId": fake_user["Id"],
            "UserName": fake_user["Name"],
            "Client": data.get("Client", "Findroid"),
            "LastActivityDate": "2026-01-01T00:00:00.0000000Z",
            "LastPlaybackCheckIn": "0001-01-01T00:00:00.0000000Z", # Added
            "DeviceName": data.get("Device", "Device"),
            "DeviceId": data.get("DeviceId", "12345"),
            "ApplicationVersion": data.get("Version", "1.0.0"),
            "IsActive": True,
            "SupportsMediaControl": False,
            "SupportsRemoteControl": False,
            "NowPlayingQueue": [],
            "NowPlayingQueueFullItems": [], # Added
            "HasCustomDeviceName": False,
            "ServerId": server_id,
            "SupportedCommands": []
        },
        "AccessToken": getattr(config, "PROXY_API_KEY", ""),
        "ServerId": server_id
    })

async def endpoint_system_info_public(request: Request):
    # Use the HOST from the header to ensure the app sees the IP it expects
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
    """Feeds native clients an empty branding config so they don't panic."""
    return JSONResponse({})