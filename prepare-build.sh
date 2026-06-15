#!/bin/bash
# prepare-build.sh
# Downloads a self-contained Python 3.12 binary (python-build-standalone) and
# installs all app dependencies into bundled-packages/ using --target so there
# are NO symlinks and NO dependency on any Python version installed on the host.
#
# Run this ONCE before `npm run build`:
#   bash prepare-build.sh
#   npm run build
#
# Re-run whenever requirements.txt changes.

set -euo pipefail
cd "$(dirname "$0")"

# ── Config ────────────────────────────────────────────────────────────────────
PY_VERSION="3.12.13"
BUILD_DATE="20260602"
ARCH="x86_64"
TARBALL="cpython-${PY_VERSION}+${BUILD_DATE}-${ARCH}-unknown-linux-gnu-install_only_stripped.tar.gz"
ENCODED_TARBALL="cpython-${PY_VERSION}%2B${BUILD_DATE}-${ARCH}-unknown-linux-gnu-install_only_stripped.tar.gz"
DOWNLOAD_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${BUILD_DATE}/${ENCODED_TARBALL}"

PYTHON_DIR="bundled-python"
PACKAGES_DIR="bundled-packages"

# ── Download standalone Python ────────────────────────────────────────────────
if [ -x "${PYTHON_DIR}/bin/python3" ]; then
    CURRENT_VER=$("${PYTHON_DIR}/bin/python3" --version 2>&1 | awk '{print $2}')
    if [ "$CURRENT_VER" = "$PY_VERSION" ]; then
        echo "✓ Bundled Python ${PY_VERSION} already present — skipping download"
    else
        echo "→ Found ${CURRENT_VER}, re-downloading ${PY_VERSION}..."
        rm -rf "$PYTHON_DIR"
    fi
fi

if [ ! -x "${PYTHON_DIR}/bin/python3" ]; then
    TMP_TAR="/tmp/amm-python-standalone.tar.gz"
    echo "→ Downloading Python ${PY_VERSION} standalone (stripped, ~25 MB)..."
    curl -fL --progress-bar -o "$TMP_TAR" "$DOWNLOAD_URL"
    echo "→ Extracting..."
    mkdir -p "$PYTHON_DIR"
    tar -xzf "$TMP_TAR" -C "$PYTHON_DIR" --strip-components=1
    rm -f "$TMP_TAR"

    # Strip unnecessary files to shrink the bundle (~30 MB saved)
    rm -rf "${PYTHON_DIR}/lib/python3.12/test"
    rm -rf "${PYTHON_DIR}/lib/python3.12/ensurepip"
    rm -rf "${PYTHON_DIR}/lib/python3.12/idlelib"
    rm -rf "${PYTHON_DIR}/lib/python3.12/tkinter"
    rm -rf "${PYTHON_DIR}/lib/python3.12/turtledemo"
    rm -rf "${PYTHON_DIR}/share"
    find "${PYTHON_DIR}" -name "*.pyc" -delete
    find "${PYTHON_DIR}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

    echo "✓ Python ${PY_VERSION} standalone ready"
fi

# ── Install packages ──────────────────────────────────────────────────────────
# --target installs flat into the directory — no venv, no symlinks, no ABI path issues.
# We use the BUNDLED Python so all compiled extensions (.so) match exactly.
REQ_HASH=$(sha256sum requirements.txt | awk '{print $1}')
HASH_FILE="${PACKAGES_DIR}/.req-hash"

if [ -d "$PACKAGES_DIR" ] && [ -f "$HASH_FILE" ] && [ "$(cat "$HASH_FILE")" = "$REQ_HASH" ]; then
    echo "✓ Packages up to date (requirements.txt unchanged)"
else
    echo "→ Installing packages into ${PACKAGES_DIR}/ ..."
    rm -rf "$PACKAGES_DIR"
    mkdir -p "$PACKAGES_DIR"

    "${PYTHON_DIR}/bin/pip3" install \
        --target="${PACKAGES_DIR}" \
        --no-deps \
        --quiet \
        pip setuptools wheel

    "${PYTHON_DIR}/bin/pip3" install \
        --target="${PACKAGES_DIR}" \
        --quiet \
        -r requirements.txt

    # Remove pip/setuptools from the bundle — not needed at runtime
    rm -rf "${PACKAGES_DIR}/pip" "${PACKAGES_DIR}/pip-"* \
           "${PACKAGES_DIR}/setuptools" "${PACKAGES_DIR}/setuptools-"* \
           "${PACKAGES_DIR}/wheel" "${PACKAGES_DIR}/wheel-"* \
           "${PACKAGES_DIR}/_distutils_hack" \
           "${PACKAGES_DIR}/__pycache__"
    find "${PACKAGES_DIR}" -name "*.pyc" -delete
    find "${PACKAGES_DIR}" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

    echo "$REQ_HASH" > "$HASH_FILE"
    echo "✓ Packages installed"
fi

# ── Bundle mkvpropedit (Matroska in-place tagging for "smart" embed mode) ──────
# So the AppImage and deb behave EXACTLY like Docker (which apt-installs
# mkvtoolnix) regardless of what is on the user's host.  We copy the binary plus
# its non-glibc shared libraries and wrap it in a launcher that sets
# LD_LIBRARY_PATH to the bundled libs ONLY — so it never affects the bundled
# Python or ffmpeg subprocesses.  The glibc core + dynamic linker are excluded
# (they must come from the host loader).
#
# If mkvpropedit is not installed on THIS build host the step is skipped: the
# app still works because the backend falls back to PATH and then to the ffmpeg
# remux, and the deb additionally declares mkvtoolnix as a dependency.
TOOLS_DIR="bundled-tools"
rm -rf "$TOOLS_DIR"
mkdir -p "$TOOLS_DIR/bin" "$TOOLS_DIR/lib"

MKV_SRC="$(command -v mkvpropedit || true)"
if [ -n "$MKV_SRC" ]; then
    echo "→ Bundling mkvpropedit from ${MKV_SRC} ..."
    cp -L "$MKV_SRC" "$TOOLS_DIR/bin/mkvpropedit.bin"
    chmod +x "$TOOLS_DIR/bin/mkvpropedit.bin"

    # Copy dynamic dependencies, excluding glibc core + the dynamic linker.
    # libstdc++ / libgcc_s ARE bundled (mkvtoolnix is C++); that's safe because
    # the launcher isolates LD_LIBRARY_PATH to mkvpropedit alone.
    EXCLUDE='^(ld-linux.*|libc|libm|libdl|libpthread|librt|libresolv|libutil)\.so'
    ldd "$MKV_SRC" | awk '/=> \// {print $3}' | sort -u | while read -r lib; do
        base="$(basename "$lib")"
        echo "$base" | grep -qE "$EXCLUDE" && continue
        cp -L "$lib" "$TOOLS_DIR/lib/" 2>/dev/null || true
    done

    # Launcher: resolve bundled libs first, then exec the real binary.
    # AMM_MKVPROPEDIT (set by electron/main.js) points here.
    cat > "$TOOLS_DIR/bin/mkvpropedit" <<'WRAP'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export LD_LIBRARY_PATH="$HERE/../lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$HERE/mkvpropedit.bin" "$@"
WRAP
    chmod +x "$TOOLS_DIR/bin/mkvpropedit"

    echo "✓ mkvpropedit bundled ($(du -sh "$TOOLS_DIR" | awk '{print $1}'))"
else
    echo "⚠ mkvpropedit not found on build host — the AppImage will rely on the"
    echo "  host's mkvtoolnix (if any) and otherwise fall back to the ffmpeg"
    echo "  remux. To bundle it: sudo apt-get install mkvtoolnix && re-run."
fi

# ── Bundle AtomicParsley (MP4/M4V/MOV in-place tagging for "smart" embed mode) ─
# Same mechanism as mkvpropedit above (review item R4): copy the binary + its
# non-glibc libs into the SHARED bundled-tools/ and wrap it in a launcher that
# isolates LD_LIBRARY_PATH to those libs only. Skipped if AtomicParsley is not on
# the build host: the backend then falls back to PATH and finally the ffmpeg
# remux, and the deb additionally declares atomicparsley as a dependency.
AP_SRC="$(command -v AtomicParsley || command -v atomicparsley || true)"
if [ -n "$AP_SRC" ]; then
    echo "→ Bundling AtomicParsley from ${AP_SRC} ..."
    cp -L "$AP_SRC" "$TOOLS_DIR/bin/AtomicParsley.bin"
    chmod +x "$TOOLS_DIR/bin/AtomicParsley.bin"

    EXCLUDE='^(ld-linux.*|libc|libm|libdl|libpthread|librt|libresolv|libutil)\.so'
    ldd "$AP_SRC" | awk '/=> \// {print $3}' | sort -u | while read -r lib; do
        base="$(basename "$lib")"
        echo "$base" | grep -qE "$EXCLUDE" && continue
        cp -L "$lib" "$TOOLS_DIR/lib/" 2>/dev/null || true
    done

    # Launcher: AMM_ATOMICPARSLEY (set by electron/main.js) points here.
    cat > "$TOOLS_DIR/bin/AtomicParsley" <<'WRAP'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export LD_LIBRARY_PATH="$HERE/../lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$HERE/AtomicParsley.bin" "$@"
WRAP
    chmod +x "$TOOLS_DIR/bin/AtomicParsley"

    echo "✓ AtomicParsley bundled"
else
    echo "⚠ AtomicParsley not found on build host — the AppImage will rely on the"
    echo "  host's atomicparsley (if any) and otherwise fall back to the ffmpeg"
    echo "  remux. To bundle it: sudo apt-get install atomicparsley && re-run."
fi

# ── Summary ───────────────────────────────────────────────────────────────────
PY_ACTUAL=$("${PYTHON_DIR}/bin/python3" --version)
echo ""
echo "Build assets ready:"
echo "  Python: ${PY_ACTUAL}  →  ${PYTHON_DIR}/"
echo "  Packages ($(du -sh "${PACKAGES_DIR}" | awk '{print $1}')) →  ${PACKAGES_DIR}/"
echo ""
echo "Now run: npm run build"
