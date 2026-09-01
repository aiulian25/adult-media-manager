# Adult Media Manager Docker Container

# ── Stage 1: the same static ffmpeg the deb/rpm/AppImage builds already ship ──
# Debian's `ffmpeg` package drags in 205 packages / ~445 MB installed — libllvm19,
# mesa, Qt, flite and the rest of the desktop graphics stack — none of which a
# `-codec copy` remux or a JPEG thumbnail touches. The static build is two
# self-contained binaries (~152 MB amd64, ~97 MB arm64) and, more importantly, is
# the EXACT build prepare-build.sh bundles into the native packages: after this,
# all four targets run byte-identical ffmpeg instead of Docker running Debian's
# and everyone else running johnvansickle's.
FROM debian:trixie-slim AS ffmpeg
ARG TARGETARCH
RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils && \
    rm -rf /var/lib/apt/lists/*
# Version and sha256 are the SAME pins as prepare-build.sh — keep them in step.
# Two URLs in that script's order: upstream serves the current build from
# releases/ and archives superseded ones under old-releases/. Only the sha decides
# which is acceptable, so a moved upstream is a hard build failure, never a
# silently different binary.
ARG FFMPEG_VERSION=7.0.2
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) SHA=abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67 ;; \
      arm64) SHA=f4149bb2b0784e30e99bdda85471c9b5930d3402014e934a5098b41d0f7201b1 ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    for url in \
        "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${TARGETARCH}-static.tar.xz" \
        "https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-${FFMPEG_VERSION}-${TARGETARCH}-static.tar.xz"; do \
      curl -fL -o /tmp/ff.tar.xz "$url" || continue; \
      if echo "$SHA  /tmp/ff.tar.xz" | sha256sum -c - >/dev/null 2>&1; then break; fi; \
      rm -f /tmp/ff.tar.xz; \
    done; \
    echo "$SHA  /tmp/ff.tar.xz" | sha256sum -c -; \
    mkdir -p /ff; \
    tar -xJf /tmp/ff.tar.xz -C /ff --strip-components=1 \
        "ffmpeg-${FFMPEG_VERSION}-${TARGETARCH}-static/ffmpeg" \
        "ffmpeg-${FFMPEG_VERSION}-${TARGETARCH}-static/ffprobe"

# ── Stage 2: the application image ───────────────────────────────────────────
FROM python:3.11-slim

# Single version source for the image: the ARG default feeds the LABEL AND the
# runtime ENV (read by app.main._resolve_app_version + docker-entrypoint.sh), so a
# release bump changes ONE line here. Keep it in sync with package.json's version.
ARG AMM_VERSION=1.12.16

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

# Install system dependencies.
# `upgrade` applies the base image's outstanding security updates — python:3.11-slim
# is rebuilt on its own cadence, so without this the image ships whatever CVEs were
# open the day that tag was cut (trivy caught the util-linux family this way).
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gosu \
        mkvtoolnix \
        atomicparsley \
    && rm -rf /var/lib/apt/lists/*

# ffmpeg/ffprobe from stage 1 rather than apt (see the note there). They land on
# PATH, which is all app/core/tools.py needs — it resolves the bare command name
# unless AMM_FFMPEG / AMM_FFPROBE override it, so no app code changes.
COPY --from=ffmpeg /ff/ffmpeg /ff/ffprobe /usr/local/bin/

# Create application directory
WORKDIR /app

# Copy the dependency files first for better layer caching. requirements.lock is
# the authoritative one (exact versions + sha256 for the whole transitive tree);
# requirements.txt ships too so the image documents its own direct deps.
COPY requirements.txt requirements.lock ./

# Install Python dependencies — REPRODUCIBLE (F5).
# --require-hashes makes pip refuse anything whose sha256 is not in the lock, so
# a rebuilt image contains a byte-identical dependency set and a compromised or
# merely newer upstream release can never enter a build silently. The same lock
# feeds the deb/rpm/AppImage bundle via prepare-build.sh, so all four targets
# ship one dependency set. Verified to resolve on cp311 x86_64 AND aarch64
# (this image is built multi-arch).
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && \
    pip uninstall -y setuptools wheel
# setuptools/wheel are build-time only — nothing in requirements.lock imports them
# at runtime, and leaving them installed ships their CVEs (and setuptools' vendored
# jaraco.*) for no benefit. prepare-build.sh strips the same three from the native
# bundle, so all four targets now ship the same runtime-only dependency set.

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
