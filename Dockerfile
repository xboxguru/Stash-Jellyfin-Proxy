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

# jellyfin-ffmpeg7 instead of the apt `ffmpeg` package: one maintained binary
# carrying NVENC + QSV + VAAPI + AMF with the matching Intel drivers bundled —
# purpose-built for this transcoding workload (see docs/Triptych.md → Hardware
# encoding). Adds ~150–300 MB (mostly the Intel stack); NVENC rides the host
# driver for free.
#
# Runtime prerequisites the operator must supply per encoder (VERTICAL_HWACCEL):
#   • NVENC       → NVIDIA Container Toolkit + `--gpus all` + host NVIDIA driver
#   • Intel QSV / VAAPI → `--device /dev/dri:/dev/dri` passthrough
#   • CPU (libx264)     → nothing; the automatic fallback when no GPU is present
ARG JELLYFIN_FFMPEG_VERSION=7.1.1-3
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash curl gosu tzdata ca-certificates && \
    arch="$(dpkg --print-architecture)" && \
    curl -fsSL -o /tmp/jellyfin-ffmpeg.deb \
        "https://github.com/jellyfin/jellyfin-ffmpeg/releases/download/v${JELLYFIN_FFMPEG_VERSION}/jellyfin-ffmpeg7_${JELLYFIN_FFMPEG_VERSION}-bookworm_${arch}.deb" && \
    apt-get install -y --no-install-recommends /tmp/jellyfin-ffmpeg.deb && \
    rm -f /tmp/jellyfin-ffmpeg.deb && \
    rm -rf /var/lib/apt/lists/*

# Point the app at the jellyfin-ffmpeg binary (overridable via env / config).
ENV FFMPEG_PATH=/usr/lib/jellyfin-ffmpeg/ffmpeg

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