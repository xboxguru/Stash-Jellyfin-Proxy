#!/bin/bash
set -e

echo "Initializing Stash-Jellyfin Proxy container..."

# 1. Handle Unraid/Docker PUID and PGID for file permissions
PUID=${PUID:-99}
PGID=${PGID:-100}

echo "Setting permissions -> PUID: $PUID, PGID: $PGID"

# 2. Safely create user/group ONLY if the numeric IDs don't already exist
if ! getent group "$PGID" >/dev/null 2>&1; then
    groupadd -g "$PGID" proxygroup || true
fi

if ! getent passwd "$PUID" >/dev/null 2>&1; then
    useradd -u "$PUID" -g "$PGID" -m -s /bin/bash proxyuser || true
fi

# 3. Fast-boot permission fix (Only touch what we need)
if [ -d "/config" ]; then
    chown "$PUID":"$PGID" /config
    [ -f "/config/stash_jellyfin_proxy.conf" ] && chown "$PUID":"$PGID" /config/stash_jellyfin_proxy.conf
    [ -f "/config/authenticated_IPs.json" ] && chown "$PUID":"$PGID" /config/authenticated_IPs.json
    [ -f "/config/stash_jellyfin_proxy.log" ] && chown "$PUID":"$PGID" /config/stash_jellyfin_proxy.log*
fi

# 4. Start the application using numeric IDs
echo "Starting Python backend..."
exec gosu "$PUID":"$PGID" python main.py