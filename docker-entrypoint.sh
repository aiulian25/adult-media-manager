#!/bin/bash
set -e

# Adult Media Manager entrypoint script
# Handles PUID/PGID/UMASK for file permissions

# Get PUID, PGID, and UMASK from environment or use defaults.
# UMASK controls the permission bits of new files created by the container.
# Default 002 → new files are 664 (group-writable), new dirs are 775.
# Use 022 if you want stricter 644/755 defaults.
PUID=${PUID:-1000}
PGID=${PGID:-1000}
UMASK=${UMASK:-002}

# Create group if it doesn't exist
if ! getent group amm > /dev/null 2>&1; then
    groupadd -g "${PGID}" amm
fi

# Create user if it doesn't exist
# NOTE: -u uses $PUID (user id), not $PGID
if ! getent passwd amm > /dev/null 2>&1; then
    useradd -u "${PUID}" -g amm -s /bin/bash -m amm
fi

# Ensure correct ownership of app data.
#
# Recurse ONLY when ownership is actually wrong. /data accumulates a thumbnails/
# directory (six JPEGs per manually-edited file), so chowning it unconditionally
# spent seconds of every startup re-setting ownership that was already correct.
#
# The test is O(top-level entries), not O(files): -maxdepth 1 covers the mount
# point itself AND its immediate children, and -quit stops at the first
# mismatch. That keeps both cases the recursion existed for:
#   • first run    — a fresh volume is root-owned, so the mount point mismatches;
#   • PUID change  — everything is owned by the previous uid, same result;
# and it also catches a backup restored as root, which lands exactly there
# (settings.json, catalog.db, thumbnails/ …).
#
# Not detected: a mismatch DEEPER than one level (a file inside thumbnails/ owned
# by root while thumbnails/ itself is not). Nothing in normal operation creates
# that — the app writes as amm, and this runs before privileges are dropped — and
# `docker exec <container> chown -R amm:amm /data` repairs it by hand.
for _dir in /data /app; do
    [ -d "$_dir" ] || continue
    if [ -n "$(find "$_dir" -maxdepth 1 \( ! -user amm -o ! -group amm \) -print -quit 2>/dev/null)" ]; then
        echo "Fixing ownership of $_dir (first run, restored data, or PUID/PGID change)…"
        chown -R amm:amm "$_dir"
    fi
done

# Grant the container user write access to the media volume.
# When the host directory is owned by root (or a different uid), rename
# operations will fail with PermissionError for every file.  We chown
# only the top-level mount-point directory itself and do NOT recurse into
# all content (which could be very slow on large libraries).  Write
# permission on the parent directory is all that is required for
# rename/move operations on Linux (rename(2) only needs write on the
# source and destination directories, not on individual files).
# A 2>/dev/null || true guard makes this a best-effort operation so the
# container still starts even if the filesystem is read-only.
for _vol in /media /downloads /organized /mnt; do
    if [ -d "$_vol" ] && [ "$(stat -c '%u' "$_vol")" != "$PUID" ]; then
        chown "$PUID:$PGID" "$_vol" 2>/dev/null || true
    fi
done

# Apply the requested umask for all subsequent child processes
umask "${UMASK}"

# Print startup message
echo "========================================="
echo "Adult Media Manager v${AMM_VERSION:-}"
echo "========================================="
echo "Running as UID:GID ${PUID}:${PGID}  umask ${UMASK}"
echo "Web UI: http://localhost:${AMM_PORT:-8887}"
echo "Data directory: /data"
echo "Media directory: /media"
echo ""
echo "⚠️  IMPORTANT:"
echo "Set TPDB_API_KEY environment variable"
echo "Get your key at: https://theporndb.net/"
echo "========================================="

# Start application with port from AMM_PORT, or execute a custom command if provided.
#
# --workers 1 is REQUIRED, not just the uvicorn default: AMM keeps match
# sessions and embed-job progress in process-local memory, so >1 worker would
# break the SSE match stream and embed-status polling (see review item P7).
# Pinning it explicitly also guards against a future change to uvicorn's default.
if [ $# -eq 0 ]; then
    exec gosu amm python -m uvicorn app.main:app \
        --host "${AMM_HOST:-0.0.0.0}" \
        --port "${AMM_PORT:-8887}" \
        --workers 1
else
    exec gosu amm "$@"
fi
