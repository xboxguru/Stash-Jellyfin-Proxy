import asyncio
import logging
import json
import config

logger = logging.getLogger(__name__)

class JellyfinDiscoveryProtocol(asyncio.DatagramProtocol):
    """Handles UDP broadcast requests from Jellyfin clients looking for a server."""
    
    def __init__(self, local_ip: str):
        self.local_ip = local_ip
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport
        logger.notice("UDP Auto-Discovery Service listening on port 7359")

    def datagram_received(self, data, addr):
        message = data.decode('utf-8', errors='ignore').strip()
        logger.debug(f"Received UDP datagram from {addr[0]}: {message}")
        
        if "who is" in message.lower():
            raw_config_ip = getattr(config, 'HOST_IP', None)
            bind_ip = raw_config_ip if raw_config_ip else self.local_ip
            
            logger.trace(f"UDP Discovery Eval -> Config HOST_IP: '{raw_config_ip}', Auto-detected local_ip: '{self.local_ip}', Final bind_ip: '{bind_ip}'")
            
            response = {
                "Address": f"http://{bind_ip}:{getattr(config, 'PROXY_PORT', 8096)}",
                "EndpointAddress": f"http://{bind_ip}:{getattr(config, 'PROXY_PORT', 8096)}",
                "Id": getattr(config, "SERVER_ID", "stash-proxy"), 
                "Name": getattr(config, "SERVER_NAME", "Stash Proxy"),
                "Version": "10.11.6" 
            }
            logger.trace(f"Answering discovery ping from {addr[0]} with response IP {bind_ip}")
            self.transport.sendto(json.dumps(response).encode('utf-8'), addr)