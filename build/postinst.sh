#!/bin/bash
# postinst.sh — runs after dpkg installs the .deb package (as root)
set -e

INSTALL_DIR="/opt/Adult Media Manager"
DESKTOP=/usr/share/applications/adult-media-manager.desktop

# ── No Python fixup needed ────────────────────────────────────────────────────
# The app ships its own self-contained python-build-standalone interpreter in
#   resources/bundled-python/
# and all pip packages flat in
#   resources/bundled-packages/
# Neither directory contains symlinks to system Python, so there is nothing to
# patch regardless of what Python version (if any) is installed on the host.

# ── Electron chrome-sandbox ───────────────────────────────────────────────────
# Prefer proper setuid-root over --no-sandbox for maximum Chromium security.
# The desktop entry also carries --no-sandbox as a belt-and-suspenders fallback.
CHROME_SANDBOX="$INSTALL_DIR/chrome-sandbox"
if [ -f "$CHROME_SANDBOX" ]; then
    chown root "$CHROME_SANDBOX" 2>/dev/null || true
    chmod 4755 "$CHROME_SANDBOX" 2>/dev/null || true
fi

# ── Patch desktop entry if --no-sandbox is missing ───────────────────────────
if [ -f "$DESKTOP" ]; then
    if ! grep -q -- '--no-sandbox' "$DESKTOP"; then
        sed -i 's|^\(Exec=.*adult-media-manager\)\(.*\)$|\1 --no-sandbox\2|' "$DESKTOP"
    fi
fi

# ── Refresh icon cache and desktop database ───────────────────────────────────
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor/ 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications/ 2>/dev/null || true
fi
