# Stash-Jellyfin Proxy v2

A high-performance Jellyfin API emulation proxy for Stash. This project is a heavily modified fork of the original Stash-Infuse proxy, optimized specifically to meet the strict metadata and scheduling requirements of **ErsatzTV** and **Tunarr**.

> **Note on Compatibility**: While this was originally designed for Infuse, significant changes have been made to the API response structures to satisfy ErsatzTV's C# backend. I have not performed testing with Infuse and make no claims that it remains compatible with mobile clients.

## Key Features
* **ErsatzTV Optimized**: Custom mappings for `o_counter` (play counts) and `created_at` timestamps ensure accurate library sorting.
* **Content Firewall**: Automatically assigns a content rating of `XXX` to all scenes to keep them isolated from mainstream movie libraries.
* **Dynamic Network Icons**: Proxies Stash Studio logos as Jellyfin "Studios" for use as watermarks/icons in linear playouts.
* **Concurrent Fetching**: Uses `httpx` and `asyncio` to handle bulk metadata requests from ErsatzTV without timeouts.
* **Web Configuration**: Manage settings, monitor active streams, and bust image caches from a built-in dashboard.

---

## ErsatzTV Integration Guide

### 1. Separation of Content
All scenes from this proxy are tagged with the **Official Rating: XXX**. To keep these separate from your Hollywood movies in ErsatzTV, use the following search filter for your Smart Collections:
* **To include only Stash scenes**: `content_rating:XXX`
* **To exclude Stash scenes**: `-content_rating:XXX`

### 2. Studio Logos (Network Watermarks)
The proxy automatically maps Stash Studios to Jellyfin Studios. To use them in ErsatzTV:
1. Run a **Scan Jellyfin** task on the Stash source.
2. Go to **Channels** -> **Edit Channel**.
3. Under **Watermark**, select your desired Stash Studio from the list.

### 3. Fixing Image Caching
ErsatzTV aggressively caches images. If you update a thumbnail in Stash and it won't refresh in ErsatzTV:
1. Go to the Proxy Web UI (**Port 8097**).
2. Click **Force Image Refresh**. This increments the internal version tag.
3. Run a **Scan Jellyfin** task in ErsatzTV.

---

## Installation (Docker Compose)

1. Create a `docker-compose.yml`:
```yaml
services:
  stash-jellyfin-proxy:
    image: YOUR_DOCKERHUB_USERNAME/stash-jellyfin-proxy:latest
    container_name: stash-jellyfin-proxy
    ports:
      - "8096:8096"   # Jellyfin API
      - "8097:8097"   # Web UI
    volumes:
      - ./config:/config
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=America/New_York
    restart: unless-stopped
```

2. Run `docker-compose up -d`.
3. Access the Web UI at `http://YOUR_IP:8097` to configure your Stash URL and API Key.
4. In ErsatzTV, add a new **Jellyfin** source using `http://YOUR_IP:8096` and the **Proxy API Key** found in the Web UI settings.

## Credits
Originally modified from the Stash-Infuse proxy project to bridge the gap between Stash and linear playout engines.

TODO:
Add HOST_IP so advertisement can work behind docker network.
Warning:  X-Forwarded-For header bypasses middleware authentication.  If exposed externally, must strip that. Investigate other options.
Implement BackgroundTasks in Starlette to execute functions after response has been sent.
Implement IMAGE_CACHE_MAX_SIZE into lur-cache or disk-cache
Refactor stash_base into config.py and replace it across all the routes
