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
        logger.info("UDP Auto-Discovery Service listening on port 7359")

    def datagram_received(self, data, addr):
        message = data.decode('utf-8', errors='ignore').strip()
        logger.debug(f"Received UDP datagram from {addr[0]}: {message}")
        
        if "who is" in message.lower():
            response = {
                "Address": f"http://{self.local_ip}:{getattr(config, 'PROXY_PORT', 8096)}",
                "EndpointAddress": f"http://{self.local_ip}:{getattr(config, 'PROXY_PORT', 8096)}",
                "Id": getattr(config, "SERVER_ID", "stash-proxy-unique-id"),
                "Name": getattr(config, "SERVER_NAME", "Stash Proxy"),
                "Version": "10.11.6" 
            }
            logger.debug(f"Answering discovery ping from {addr[0]} with cached IP {self.local_ip}")
            self.transport.sendto(json.dumps(response).encode('utf-8'), addr)