#!/bin/bash
# postrm.sh — runs after dpkg removes the .deb package (as root)
set -e

# Refresh icon cache and desktop database so the launcher entry disappears
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t /usr/share/icons/hicolor/ 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database /usr/share/applications/ 2>/dev/null || true
fi
