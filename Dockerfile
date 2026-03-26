FROM python:3.11-slim-bookworm

LABEL maintainer="xboxguru"
LABEL description="Jellyfin API emulation proxy for Stash"
LABEL version="2.0.0"

ARG BUILD_VERSION="v2.1-dev"
ENV APP_VERSION=$BUILD_VERSION

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 1. Added wget and unzip to the install list
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl gosu tzdata wget unzip && \
    rm -rf /var/lib/apt/lists/*

# 2. Install modular dependencies
RUN pip install --no-cache-dir \
    hypercorn starlette requests httpx

RUN mkdir -p /app /config && chmod 755 /app /config

# 3. Download and extract the pre-compiled Jellyfin Web UI during the build
RUN wget -q https://nyc1.mirror.jellyfin.org/main/server/windows/stable/v10.9.11/amd64/jellyfin_10.9.11-amd64.zip -O /tmp/jellyfin.zip && \
    unzip -q /tmp/jellyfin.zip "jellyfin-web/*" -d /app/ && \
    rm /tmp/jellyfin.zip

# 4. Copy the new modular structure
COPY api/ /app/api/
COPY core/ /app/core/
COPY templates/ /app/templates/
COPY *.py /app/
COPY docker-entrypoint.sh /docker-entrypoint.sh
COPY requirements.txt /app/

RUN chmod +x /docker-entrypoint.sh

WORKDIR /app

# Match your existing ports
EXPOSE 8096 8097 7539/udp

VOLUME ["/config"]

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "main.py"]