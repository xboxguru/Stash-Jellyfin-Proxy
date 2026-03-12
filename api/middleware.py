import re
import urllib.parse
import logging
import config
import state

logger = logging.getLogger(__name__)

# Endpoints that do not require the PROXY_API_KEY
PUBLIC_ENDPOINTS = {
    "/system/info/public", 
    "/web/index.html", 
    "/health", 
    "/", 
    "/users/authenticatebyname", 
    "/system/ping",
    "/users/public",
    "/quickconnect/initiate",
    "/quickconnect/enabled"
}
PUBLIC_PREFIXES = ["/web/", "/assets/", "/api/"]

def get_client_ip(scope) -> str:
    """Extract client IP safely, accounting for reverse proxies."""
    for name, value in scope.get("headers", []):
        if name.decode("latin1").lower() == "x-forwarded-for":
            return value.decode("latin1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "127.0.0.1"

class AuthenticationMiddleware:
    """ASGI middleware that validates PROXY_API_KEY on protected endpoints."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        # We only care about HTTP requests
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        path_lower = path.lower()
        client_ip = get_client_ip(scope)
        
        # 1. STRIP PREFIXES: Fixes Findroid, Jellycon, and ErsatzTV prepending /emby or /jellyfin
        # We ensure we only strip if it's a folder prefix (ending in /) or the exact path
        if path_lower.startswith("/emby/"):
            scope["path"] = path[5:]
        elif path_lower == "/emby":
            scope["path"] = "/"
        elif path_lower.startswith("/jellyfin/"):
            scope["path"] = path[9:]
        elif path_lower == "/jellyfin":
            scope["path"] = "/"

        # Update local variables after potentially stripping prefix
        path = scope.get("path", "")
        path_lower = path.lower()

        # 2. Check if the path is allowed without authentication
        is_public = path_lower in PUBLIC_ENDPOINTS
        if not is_public:
            for prefix in PUBLIC_PREFIXES:
                if path_lower.startswith(prefix):
                    is_public = True
                    break

        # NEW: Allow image and video stream requests ONLY if the client or the referring website is authenticated
        if "/images/" in path_lower or "/videos/" in path_lower:
            import state
            auth_ips = getattr(state, "authenticated_ips", set())
            
            # 1. Did this specific device already authenticate directly?
            if client_ip in auth_ips:
                is_public = True
            else:
                # 2. Did an authenticated server (like Tunarr) tell them to load this image?
                for key, value in scope.get("headers", []):
                    key_lower = key.decode("latin1").lower()
                    if key_lower in ["referer", "origin"]:
                        header_val = value.decode("utf-8", errors="ignore")
                        # Check if any of our trusted IPs appear in the Referer URL
                        if any(auth_ip in header_val for auth_ip in auth_ips):
                            is_public = True
                            break      

        if is_public:
            # 1. UI SECURITY: Check if this is a protected dashboard API route
            is_ui_api = path_lower.startswith("/api/")
            
            # We ONLY whitelist the login and logout endpoints. Everything else requires a cookie.
            ui_public_routes = {"/api/login", "/api/logout"}
            
            if is_ui_api and path_lower not in ui_public_routes and getattr(config, "REQUIRE_AUTH_FOR_CONFIG", False):
                import state
                token = ""
                # Search the headers for our custom 'ui_session' cookie
                for key, value in scope.get("headers", []):
                    if key.decode().lower() == "cookie":
                        cookies = value.decode()
                        for cookie in cookies.split(";"):
                            if cookie.strip().startswith("ui_session="):
                                token = cookie.strip()[11:]
                
                # If the token is missing or invalid, block access to the API data
                if not token or token not in getattr(state, "ui_sessions", set()):
                    response_body = b'{"error": "UI Authentication Required"}'
                    await send({
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"content-length", str(len(response_body)).encode()],
                        ],
                    })
                    await send({"type": "http.response.body", "body": response_body})
                    return

            # Passed! Allow the route to execute.
            await self.app(scope, receive, send)
            return

        # 3. Extract the token from query string or headers
        token = None
        
        # Check URL query string first (ErsatzTV uses this sometimes)
        query_bytes = scope.get("query_string", b"")
        if query_bytes:
            parsed_query = {k.lower(): v for k, v in urllib.parse.parse_qs(query_bytes.decode("utf-8")).items()}
            token = parsed_query.get("api_key", [None])[0] or parsed_query.get("token", [None])[0]

# Check headers if not found in query string
        if not token:
            for key, value in scope.get("headers", []):
                key_lower = key.decode().lower()
                value_str = value.decode("utf-8", errors="ignore")

                # The C# Jellyfin SDK (ErsatzTV) passes keys in X-Emby-Authorization!
                if key_lower in ["x-emby-token", "x-mediabrowser-token"]:
                    token = value_str
                elif key_lower in ["authorization", "x-emby-authorization"]:
                    if value_str.startswith("Bearer "):
                        token = value_str[7:]
                    elif "token=" in value_str.lower():
                        # Make quotes optional and case-insensitive
                        match = re.search(r'token="?([^",\s]+)"?', value_str, re.IGNORECASE)
                        if match:
                            token = match.group(1)
                
                # ONLY stop searching the headers if we actually found a token
                if token:
                    break

        # Strip any literal quotes just in case the user typed them in the UI
        if token:
            token = token.strip('"').strip("'").strip()

        # 4. Validate the token against our configurable PROXY_API_KEY
        expected_key = getattr(config, "PROXY_API_KEY", "").strip()
        clean_token = token.strip('"').strip("'").strip() if token else None
        
        if clean_token and clean_token == expected_key:
            import state
            state.stats["auth_success"] += 1
            state.stats["unique_ips_today"].add(client_ip)
            
            # NEW: Remember this IP as a trusted client for password-less image requests
            if not hasattr(state, "authenticated_ips"):
                state.authenticated_ips = set()
            
            # Save to JSON if this is a brand new IP
            if client_ip not in state.authenticated_ips:
                state.authenticated_ips.add(client_ip)
                if hasattr(state, "save_auth_ips"):
                    state.save_auth_ips(state.authenticated_ips)
            
            if not path_lower.startswith("/api/") and not path_lower.startswith("/web/"):
                query = scope.get('query_string', b'').decode('utf-8')
                full_url = f"{path}?{query}" if query else path
                logger.info(f"JELLYFIN CLIENT -> {scope['method']} {full_url}")
            
            await self.app(scope, receive, send)
            return

        # 5. If we get here, authentication failed. Reject the request.
        import state
        state.stats["auth_failed"] += 1
        
        # Enhanced debugging logging so we can see EXACTLY why a client failed
        if token:
            logger.warning(f"Unauthorized access attempt to {path} from {client_ip} | Reason: Token mismatch. Received: '{token}' | Expected: '{expected_key}'")
        else:
            # DEBUG: Print the raw headers to see how Jellycon is hiding the token
            safe_headers = {k.decode('latin1'): v.decode('utf-8', errors='ignore') for k, v in scope.get("headers", [])}
            logger.warning(f"Unauthorized access attempt to {path} from {client_ip} | Reason: No token provided. Headers: {safe_headers}")
        
        response_body = b'{"error": "Unauthorized"}'
        
        response_body = b'{"error": "Unauthorized"}'
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"content-length", str(len(response_body)).encode()],
            ],
        })
        await send({"type": "http.response.body", "body": response_body})