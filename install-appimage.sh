#!/bin/bash
# install-appimage.sh — Installs the Adult Media Manager AppImage system-wide.
# Run with: bash install-appimage.sh [path/to/Adult_Media_Manager-*.AppImage]
#
# What it does:
#   1. Copies the AppImage to ~/.local/bin/
#   2. Installs icons to ~/.local/share/icons/hicolor/
#   3. Writes a .desktop entry to ~/.local/share/applications/
#   4. Refreshes the icon cache and desktop database
#
# To uninstall:
#   rm ~/.local/bin/adult-media-manager.AppImage
#   rm ~/.local/share/applications/adult-media-manager.desktop
#   find ~/.local/share/icons/hicolor -name "adult-media-manager.png" -delete

set -euo pipefail

APPIMAGE="${1:-}"

# ── Find the AppImage ─────────────────────────────────────────────────────────
if [ -z "$APPIMAGE" ]; then
    APPIMAGE="$(ls Adult_Media_Manager-*.AppImage 2>/dev/null | head -1)"
fi

if [ -z "$APPIMAGE" ] || [ ! -f "$APPIMAGE" ]; then
    echo "Usage: $0 <path/to/Adult_Media_Manager-*.AppImage>"
    echo "Or run this script from the directory containing the AppImage."
    exit 1
fi

APPIMAGE="$(realpath "$APPIMAGE")"
echo "Installing: $APPIMAGE"

# ── Directories ───────────────────────────────────────────────────────────────
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"
ICON_BASE="$HOME/.local/share/icons/hicolor"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$BIN_DIR" "$APPS_DIR"

# ── Install AppImage ──────────────────────────────────────────────────────────
DEST="$BIN_DIR/adult-media-manager.AppImage"
cp "$APPIMAGE" "$DEST"
chmod +x "$DEST"
echo "  → Installed AppImage to: $DEST"

# ── Install icons ─────────────────────────────────────────────────────────────
ICON_SIZES=(16 24 32 48 64 96 128 256 512)
for SIZE in "${ICON_SIZES[@]}"; do
    SRC="$SCRIPT_DIR/build/icons/${SIZE}x${SIZE}.png"
    if [ -f "$SRC" ]; then
        SIZE_DIR="$ICON_BASE/${SIZE}x${SIZE}/apps"
        mkdir -p "$SIZE_DIR"
        cp "$SRC" "$SIZE_DIR/adult-media-manager.png"
    fi
done

# Refresh icon cache (non-fatal if missing)
gtk-update-icon-cache -f -t "$ICON_BASE" 2>/dev/null || true
echo "  → Icons installed"

# ── Write .desktop entry ──────────────────────────────────────────────────────
cat > "$APPS_DIR/adult-media-manager.desktop" <<DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=Adult Media Manager
GenericName=Adult Media Organizer
Comment=Smart metadata organizer for adult content
Exec=env APPIMAGE_EXTRACT_AND_RUN=1 ${DEST} --no-sandbox %U
Icon=adult-media-manager
Categories=AudioVideo;Video;Utility;
Terminal=false
StartupNotify=true
StartupWMClass=AdultMediaManager
DESKTOP

chmod 644 "$APPS_DIR/adult-media-manager.desktop"
update-desktop-database "$APPS_DIR" 2>/dev/null || true
echo "  → Desktop entry installed"

# ── Ensure ~/.local/bin is in PATH ───────────────────────────────────────────
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    echo "NOTE: $BIN_DIR is not in your PATH."
    echo "Add this line to your ~/.bashrc or ~/.profile:"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "Done! Launch 'Adult Media Manager' from your app launcher or run:"
echo "  $DEST"
