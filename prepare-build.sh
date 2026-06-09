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

# ── Summary ───────────────────────────────────────────────────────────────────
PY_ACTUAL=$("${PYTHON_DIR}/bin/python3" --version)
echo ""
echo "Build assets ready:"
echo "  Python: ${PY_ACTUAL}  →  ${PYTHON_DIR}/"
echo "  Packages ($(du -sh "${PACKAGES_DIR}" | awk '{print $1}')) →  ${PACKAGES_DIR}/"
echo ""
echo "Now run: npm run build"
