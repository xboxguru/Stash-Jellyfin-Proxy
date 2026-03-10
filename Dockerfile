FROM python:3.11-slim-bookworm

LABEL maintainer="Stash-Jellyfin Proxy"
LABEL description="Jellyfin API emulation proxy for Stash media server"
LABEL version="5.01"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    gosu \
    tzdata \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
    hypercorn \
    starlette \
    requests \
    httpx \
    Pillow

RUN mkdir -p /app /config && \
    chmod 755 /app /config

# Copy the entire modular directory structure
COPY . /app/

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

WORKDIR /app

ENV PUID=1000 \
    PGID=1000 \
    TZ=UTC \
    PROXY_BIND=0.0.0.0 \
    PROXY_PORT=8096 \
    UI_PORT=8097 \
    LOG_DIR=/config

EXPOSE 8096 8097

VOLUME ["/config"]

ENTRYPOINT ["/docker-entrypoint.sh"]

# Point to our new main loop
CMD ["python", "/app/main.py"]