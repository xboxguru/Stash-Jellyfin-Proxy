import re
import urllib.parse
import logging
import time
import ipaddress
import config
import state

logger = logging.getLogger(__name__)

PUBLIC_ENDPOINTS = {
    "/", "/system/info/public", "/system/info", "/public/system/info",
    "/web/index.html", "/health", "/users/authenticatebyname", "/system/ping",
    "/users/public", "/quickconnect/initiate", "/quickconnect/enabled",
    "/quickconnect/connect", "/quickconnect/authorize", "/users/authenticatewithquickconnect", "/favicon.ico", "/branding/configuration", 
    "/clientlog/document"
}
PUBLIC_PREFIXES = ["/web/", "/assets/", "/api/"]

def _ip_matches(client_ip: str, entries: list) -> bool:
    """Check if client_ip matches any entry in the list — exact IPs or CIDR ranges (e.g. 192.168.1.0/24)."""
    try:
        addr = ipaddress.ip_address(client_ip)
        for entry in entries:
            try:
                net = ipaddress.ip_network(str(entry), strict=False)
                if addr in net:
                    return True
            except ValueError:
                continue
    except ValueError:
        pass
    return False

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
            
        static_ips = getattr(config, "AUTHENTICATED_IPS", [])
        if _ip_matches(client_ip, static_ips): return True
            
        auth_ips = getattr(state, "authenticated_ips", {})
        if isinstance(auth_ips, set): 
            auth_ips = {ip: time.time() for ip in auth_ips}
            state.authenticated_ips = auth_ips
        
        if client_ip in auth_ips:
            state.authenticated_ips[client_ip] = time.time()
            return True
            
        all_allowed = set(auth_ips.keys()).union(set(static_ips))
        for key, value in scope.get("headers", []):
            if key.decode("latin1").lower() in ["referer", "origin"]:
                header_val = value.decode("utf-8", errors="ignore")
                if any(re.search(rf"\b{re.escape(ip)}\b", header_val) for ip in all_allowed):
                    return True
        return False

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        start_time = time.time()
        
        original_path = scope.get("path", "")
        query_string = scope.get("query_string", b"").decode("utf-8", errors="ignore")
        method = scope.get("method", "UNK")
        client_ip = get_client_ip(scope)
        
        # --- EXTREME CATCH-ALL LOGGING ---
        full_url_for_log = f"{original_path}?{query_string}" if query_string else original_path
        logger.debug(f"[[ INBOUND ]] {method} {full_url_for_log} | IP: {client_ip}")
        # ---------------------------------

        path_lower = original_path.lower()
        
        # FIX: Strip trailing slashes so Jellyfin clients don't 404/401 randomly
        if path_lower != "/" and path_lower.endswith("/"):
            path_lower = path_lower[:-1]

        if not path_lower.startswith("/web/") and not path_lower.startswith("/assets/"):
            scope["path"] = path_lower

        if method == "OPTIONS":
            return await self.app(scope, receive, send)

        if path_lower.startswith("/emby/"): scope["path"] = path_lower[5:]
        elif path_lower == "/emby": scope["path"] = "/"
        elif path_lower.startswith("/jellyfin/"): scope["path"] = path_lower[9:]
        elif path_lower == "/jellyfin": scope["path"] = "/"
        path_lower = scope["path"]
        
        logger.debug(f"Request: {method} {original_path} (Routed as: {path_lower}) from {client_ip}")

        is_public = path_lower in PUBLIC_ENDPOINTS or any(path_lower.startswith(p) for p in PUBLIC_PREFIXES)
        
        if not is_public and self._is_image_or_video_authorized(path_lower, client_ip, scope):
            is_public = True

        if is_public:
            is_ui_api = path_lower.startswith("/api/")
            if is_ui_api and path_lower not in {"/api/login", "/api/logout"} and getattr(config, "REQUIRE_AUTH_FOR_CONFIG", False):
                token = next((v.decode().split("ui_session=")[1].split(";")[0] for k, v in scope.get("headers", []) if k.decode().lower() == "cookie" and "ui_session=" in v.decode()), None)
                if not token or token not in getattr(state, "ui_sessions", set()):
                    logger.warning(f"Unauthorized Web UI access attempt from {client_ip}")
                    return await self._send_unauthorized(send)

            response = await self._process_request(scope, receive, send, start_time)
            return response

        token = self._extract_jellyfin_token(scope)
        expected_key = getattr(config, "PROXY_API_KEY", "").strip()
        
        if token and token == expected_key:
            state.stats["auth_success"] += 1
            state.stats["unique_ips_today"].add(client_ip)
            static_ips = getattr(config, "AUTHENTICATED_IPS", [])
            if not _ip_matches(client_ip, static_ips):
                if not hasattr(state, "authenticated_ips") or isinstance(state.authenticated_ips, set):
                    state.authenticated_ips = {}
                state.authenticated_ips[client_ip] = time.time()
                
            response = await self._process_request(scope, receive, send, start_time)
            return response

        state.stats["auth_failed"] += 1
        logger.warning(f"Unauthorized API request to {original_path} from {client_ip}")
        await self._send_unauthorized(send)
        
    async def _process_request(self, scope, receive, send, start_time):
        try:
            response = await self.app(scope, receive, send)
            elapsed = time.time() - start_time
            # Optional: Keep the debug log if you want to see how long it took, 
            # but the INBOUND error log above is the real star of the show.
            logger.debug(f"Completed {scope['method']} {scope['path']} in {elapsed:.3f}s")
            return response
        except Exception as e:
            logger.error(f"Server Error during {scope['method']} {scope['path']}: {e}", exc_info=True)
            raise

    async def _send_unauthorized(self, send):
        response_body = b'{"error": "Unauthorized"}'
        await send({
            "type": "http.response.start", 
            "status": 401, 
            "headers": [[b"content-type", b"application/json"], [b"content-length", str(len(response_body)).encode()]]
        })
        await send({"type": "http.response.body", "body": response_body})