FROM python:3.11-slim-bookworm

LABEL maintainer="xboxguru"
LABEL description="Jellyfin API emulation proxy for Stash"
LABEL version="2.0.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl gosu tzdata && \
    rm -rf /var/lib/apt/lists/*

# Install modular dependencies
RUN pip install --no-cache-dir \
    hypercorn starlette requests httpx Pillow

RUN mkdir -p /app /config && chmod 755 /app /config

# Copy the new modular structure
COPY api/ /app/api/
COPY core/ /app/core/
COPY templates/ /app/templates/
COPY *.py /app/
COPY docker-entrypoint.sh /docker-entrypoint.sh
COPY requirements.txt /app/

RUN chmod +x /docker-entrypoint.sh

WORKDIR /app

# Match your existing ports [cite: 1]
EXPOSE 8096 8097

VOLUME ["/config"]

ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "main.py"]