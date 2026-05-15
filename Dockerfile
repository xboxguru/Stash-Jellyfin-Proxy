# Stage 1: Steal the compiled Web UI from the official Jellyfin image
FROM jellyfin/jellyfin:10.9.11 AS jellyfin-base

# Stage 2: Build our actual Proxy image
FROM python:3.11-slim-bookworm

LABEL maintainer="xboxguru"
LABEL description="Jellyfin API emulation proxy for Stash"
LABEL version="2.0.0"

ARG BUILD_VERSION="v2.1-dev"
ENV APP_VERSION=$BUILD_VERSION

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl gosu tzdata ffmpeg && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app /config && chmod 755 /app /config

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY --from=jellyfin-base /jellyfin/jellyfin-web /app/jellyfin-web

COPY api/ /app/api/
COPY core/ /app/core/
COPY templates/ /app/templates/
COPY *.py /app/
COPY docker-entrypoint.sh /docker-entrypoint.sh

RUN chmod +x /docker-entrypoint.sh

WORKDIR /app

# Match your existing ports, plus UDP Discovery
EXPOSE 8096 8097 7359/udp

VOLUME ["/config"]

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "main.py"]