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
# electron-builder emits:  Exec="/opt/Adult Media Manager/adult-media-manager" %U
# The flag MUST go *after* the closing quote of the executable path, otherwise it
# ends up inside the quotes (Exec="…adult-media-manager --no-sandbox") and GLib's
# g_desktop_app_info parser rejects the whole entry — leaving the app installed
# but invisible to GNOME Shell's app grid and search. Match the quoted executable
# (or a bare unquoted token) explicitly so the flag is appended outside it.
if [ -f "$DESKTOP" ]; then
    if ! grep -q -- '--no-sandbox' "$DESKTOP"; then
        sed -i -E 's@^(Exec=("[^"]*adult-media-manager"|[^ ]*adult-media-manager))(.*)$@\1 --no-sandbox\3@' "$DESKTOP"
    fi
fi

# ── Refresh icon cache and desktop database ───────────────────────────────────
# Icon cache refresh (always try, suppress non-critical errors)
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor/ 2>/dev/null || true
fi

# Desktop database update (KDE uses kbuildsycoca5, others use update-desktop-database)
# Try KDE first (more common on modern systems), then fall back to generic tool
if command -v kbuildsycoca5 >/dev/null 2>&1; then
    kbuildsycoca5 >/dev/null 2>&1 || true
elif command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications/ 2>/dev/null || true
fi

# Notify user to refresh if on a graphical session
if [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
    echo "Adult Media Manager installed. The application menu may need a refresh."
    echo "Log out and back in, or press Alt+F2 and type 'kquitapp5 plasmashell; plasmashell &' (KDE)"
fi
