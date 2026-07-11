# Adult Media Manager Docker Container
FROM python:3.11-slim

# Single version source for the image: the ARG default feeds the LABEL AND the
# runtime ENV (read by app.main._resolve_app_version + docker-entrypoint.sh), so a
# release bump changes ONE line here. Keep it in sync with package.json's version.
ARG AMM_VERSION=1.8.0

LABEL maintainer="Adult Media Manager <app@adultmediamanager.local>"
LABEL description="Adult media metadata organizer with TPDB integration"
LABEL version="${AMM_VERSION}"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AMM_HOST=0.0.0.0 \
    AMM_PORT=8887 \
    PUID=1000 \
    PGID=1000 \
    DATA_DIR=/data \
    AMM_VERSION=${AMM_VERSION}

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gosu \
        ffmpeg \
        mkvtoolnix \
        atomicparsley \
    && rm -rf /var/lib/apt/lists/*

# Create application directory
WORKDIR /app

# Copy requirements first for better layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Create non-root user and necessary directories
RUN groupadd -g 1000 amm && \
    useradd -u 1000 -g amm -s /bin/bash -m amm && \
    mkdir -p /data /media && \
    chown -R amm:amm /app /data /media

# Copy entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Expose web UI port (matches AMM_PORT env var)
EXPOSE $AMM_PORT

# Health check - verify API is responding
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${AMM_PORT}/api/health || exit 1

# Volume for persistent data
VOLUME ["/data", "/media"]

# Set entrypoint
ENTRYPOINT ["docker-entrypoint.sh"]

# Default command — port is read from AMM_PORT at runtime by docker-entrypoint.sh
CMD []
