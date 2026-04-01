import re
import urllib.parse
import logging
import time 
import config
import state

logger = logging.getLogger(__name__)

PUBLIC_ENDPOINTS = {
    "/",
    "/system/info/public", 
    "/system/info",
    "/public/system/info",
    "/web/index.html", 
    "/health", 
    "/users/authenticatebyname", 
    "/system/ping",
    "/users/public",
    "/quickconnect/initiate",
    "/quickconnect/enabled",
    "/favicon.ico",
    "/branding/configuration",
    "/clientlog/document"
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

    def _extract_jellyfin_token(self, scope) -> str | None:
        query_bytes = scope.get("query_string", b"")
        if query_bytes:
            parsed_query = {k.lower(): v for k, v in urllib.parse.parse_qs(query_bytes.decode("utf-8")).items()}
            token = parsed_query.get("api_key", [None])[0] or parsed_query.get("token", [None])[0]
            if token: return token.strip('"').strip("'").strip()

        for key, value in scope.get("headers", []):
            key_lower = key.decode().lower()
            value_str = value.decode("utf-8", errors="ignore")

            if key_lower in ["x-emby-token", "x-mediabrowser-token"]:
                return value_str.strip('"').strip("'").strip()
            elif key_lower in ["authorization", "x-emby-authorization"]:
                if value_str.startswith("Bearer "):
                    return value_str[7:].strip('"').strip("'").strip()
                elif "token=" in value_str.lower():
                    match = re.search(r'token="?([^",\s]+)"?', value_str, re.IGNORECASE)
                    if match: return match.group(1).strip('"').strip("'").strip()
        return None

    def _is_image_or_video_authorized(self, path_lower: str, client_ip: str, scope) -> bool:
        if "/images/" not in path_lower and "/videos/" not in path_lower:
            return False
            
        auth_ips = getattr(state, "authenticated_ips", {})
        if isinstance(auth_ips, set): 
            auth_ips = {ip: time.time() for ip in auth_ips}
        
        if client_ip in auth_ips:
            state.authenticated_ips[client_ip] = time.time()
            return True
            
        for key, value in scope.get("headers", []):
            if key.decode("latin1").lower() in ["referer", "origin"]:
                header_val = value.decode("utf-8", errors="ignore")
                if any(re.search(rf"\b{re.escape(auth_ip)}\b", header_val) for auth_ip in auth_ips.keys()):
                    return True
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        original_path = scope.get("path", "")
        path_lower = original_path.lower()
        method = scope.get("method", "UNK")
        client_ip = get_client_ip(scope)

        if not path_lower.startswith("/web/") and not path_lower.startswith("/assets/"):
            scope["path"] = path_lower

        if method == "OPTIONS":
            return await self.app(scope, receive, send)

        if path_lower.startswith("/emby/"): scope["path"] = path_lower[5:]
        elif path_lower == "/emby": scope["path"] = "/"
        elif path_lower.startswith("/jellyfin/"): scope["path"] = path_lower[9:]
        elif path_lower == "/jellyfin": scope["path"] = "/"
        path_lower = scope["path"]

        is_public = path_lower in PUBLIC_ENDPOINTS or any(path_lower.startswith(p) for p in PUBLIC_PREFIXES)
        
        if not is_public and self._is_image_or_video_authorized(path_lower, client_ip, scope):
            is_public = True

        if is_public:
            is_ui_api = path_lower.startswith("/api/")
            if is_ui_api and path_lower not in {"/api/login", "/api/logout"} and getattr(config, "REQUIRE_AUTH_FOR_CONFIG", False):
                token = next((v.decode().split("ui_session=")[1].split(";")[0] for k, v in scope.get("headers", []) if k.decode().lower() == "cookie" and "ui_session=" in v.decode()), None)
                if not token or token not in getattr(state, "ui_sessions", set()):
                    logger.warning(f"Unauthorized Web UI access attempt from {client_ip}")
                    await self._send_unauthorized(send)
                    return
            return await self.app(scope, receive, send)

        token = self._extract_jellyfin_token(scope)
        expected_key = getattr(config, "PROXY_API_KEY", "").strip()
        
        if token and token == expected_key:
            state.stats["auth_success"] += 1
            state.stats["unique_ips_today"].add(client_ip)
            if not hasattr(state, "authenticated_ips") or isinstance(state.authenticated_ips, set):
                state.authenticated_ips = {}
            state.authenticated_ips[client_ip] = time.time()
            return await self.app(scope, receive, send)

        state.stats["auth_failed"] += 1
        logger.warning(f"Unauthorized access attempt to {original_path} from {client_ip}")
        await self._send_unauthorized(send)

    async def _send_unauthorized(self, send):
        response_body = b'{"error": "Unauthorized"}'
        await send({
            "type": "http.response.start", 
            "status": 401, 
            "headers": [[b"content-type", b"application/json"], [b"content-length", str(len(response_body)).encode()]]
        })
        await send({"type": "http.response.body", "body": response_body})