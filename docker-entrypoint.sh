#!/bin/bash
set -e

echo "Initializing Stash-Jellyfin Proxy container..."

# 1. Handle Unraid/Docker PUID and PGID for file permissions
PUID=${PUID:-99}
PGID=${PGID:-100}

echo "Setting permissions -> PUID: $PUID, PGID: $PGID"

# Create a group if it doesn't exist
if ! getent group abc >/dev/null; then
    groupadd -g "$PGID" abc
fi

# Create a user if it doesn't exist
if ! getent passwd abc >/dev/null; then
    useradd -u "$PUID" -g "$PGID" -m -s /bin/bash abc
fi

# 2. Fix permissions on the config folder before starting
if [ -d "/config" ]; then
    chown "$PUID":"$PGID" /config
    # Only touch the specific files we own, ignoring deep subdirectories
    [ -f "/config/stash_jellyfin_proxy.conf" ] && chown "$PUID":"$PGID" /config/stash_jellyfin_proxy.conf
    [ -f "/config/stash_jellyfin_proxy.log" ] && chown "$PUID":"$PGID" /config/stash_jellyfin_proxy.log*
fi

# 3. Start the application
# We use 'gosu' to drop root privileges and run the app as the Unraid user.
# config.py will automatically generate the config file if it doesn't exist!
echo "Starting Python backend..."
exec gosu abc python main.py