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
# Target CPU architecture (F18). x86_64 (default) or aarch64:
#   bash prepare-build.sh                      → x64 assets (unchanged)
#   AMM_BUILD_ARCH=aarch64 bash prepare-build.sh → arm64 assets (cross-stage)
# The bundled dirs are OVERWRITTEN per run — build the matching electron
# artifact immediately after each pass:
#   bash prepare-build.sh && npm run build
#   AMM_BUILD_ARCH=aarch64 bash prepare-build.sh && npm run build:arm64
ARCH="${AMM_BUILD_ARCH:-x86_64}"
case "$ARCH" in x86_64|aarch64) ;; *) echo "AMM_BUILD_ARCH must be x86_64 or aarch64"; exit 1;; esac
HOST_ARCH="$(uname -m)"
CROSS=""
[ "$ARCH" != "$HOST_ARCH" ] && CROSS=1
TARBALL="cpython-${PY_VERSION}+${BUILD_DATE}-${ARCH}-unknown-linux-gnu-install_only_stripped.tar.gz"
ENCODED_TARBALL="cpython-${PY_VERSION}%2B${BUILD_DATE}-${ARCH}-unknown-linux-gnu-install_only_stripped.tar.gz"
DOWNLOAD_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${BUILD_DATE}/${ENCODED_TARBALL}"

PYTHON_DIR="bundled-python"
PACKAGES_DIR="bundled-packages"

echo "Target arch: ${ARCH}$( [ -n "$CROSS" ] && echo ' (cross-staging on '"$HOST_ARCH"')' )"

# ── Download standalone Python ────────────────────────────────────────────────
# Arch marker instead of executing the binary — a cross-staged aarch64 python
# cannot run on the build host.
PY_MARKER="${PYTHON_DIR}/.amm-py"
WANT_PY="${PY_VERSION}-${ARCH}"
if [ -x "${PYTHON_DIR}/bin/python3" ]; then
    if [ "$(cat "$PY_MARKER" 2>/dev/null)" = "$WANT_PY" ]; then
        echo "✓ Bundled Python ${WANT_PY} already present — skipping download"
    else
        echo "→ Bundled python is not ${WANT_PY} — re-downloading..."
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

    echo "$WANT_PY" > "$PY_MARKER"
    echo "✓ Python ${PY_VERSION} standalone (${ARCH}) ready"
fi

# ── Install packages ──────────────────────────────────────────────────────────
# --target installs flat into the directory — no venv, no symlinks, no ABI path issues.
# We use the BUNDLED Python so all compiled extensions (.so) match exactly.
REQ_HASH="$(sha256sum requirements.txt | awk '{print $1}')-${ARCH}"
HASH_FILE="${PACKAGES_DIR}/.req-hash"

if [ -d "$PACKAGES_DIR" ] && [ -f "$HASH_FILE" ] && [ "$(cat "$HASH_FILE")" = "$REQ_HASH" ]; then
    echo "✓ Packages up to date (requirements.txt unchanged, ${ARCH})"
else
    echo "→ Installing packages into ${PACKAGES_DIR}/ (${ARCH}) ..."
    rm -rf "$PACKAGES_DIR"
    mkdir -p "$PACKAGES_DIR"

    if [ -n "$CROSS" ]; then
        # Cross-stage (F18): the bundled aarch64 python can't run here, so the
        # HOST python downloads prebuilt manylinux aarch64 wheels only — no
        # source builds can slip in (--only-binary=:all: makes a missing wheel
        # a HARD ERROR rather than a silently wrong-arch package).
        python3 -m pip install \
            --target="${PACKAGES_DIR}" \
            --platform manylinux2014_aarch64 \
            --only-binary=:all: \
            --python-version "${PY_VERSION%.*}" \
            --implementation cp \
            --quiet \
            -r requirements.txt
    else
        "${PYTHON_DIR}/bin/pip3" install \
            --target="${PACKAGES_DIR}" \
            --no-deps \
            --quiet \
            pip setuptools wheel

        "${PYTHON_DIR}/bin/pip3" install \
            --target="${PACKAGES_DIR}" \
            --quiet \
            -r requirements.txt
    fi

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
if [ -n "$CROSS" ]; then
    echo "⚠ Cross-staging ${ARCH}: skipping mkvpropedit/AtomicParsley bundling (host"
    echo "  binaries are ${HOST_ARCH}; ldd cannot resolve cross-arch). deb/rpm declare"
    echo "  them as dependencies and the embed planner falls back to the ffmpeg remux."
    MKV_SRC=""
fi
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
[ -n "$CROSS" ] && AP_SRC=""
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

# ── Bundle ffmpeg + ffprobe (remux embed mode, probes, thumbnails, pHash) ──────
# Remux is the DEFAULT metadata mode, so ffmpeg must be guaranteed on every
# target: Docker apt-installs it, deb/rpm declare it as a dependency, and the
# AppImage — "any distro, no root" — gets it bundled here (F2).
#
# PREFERRED: the johnvansickle STATIC build — two self-contained binaries, no
# shared libs, no launcher, no host-lib drift, ~160 MB unpacked vs ~200 MB of
# ldd-copied system libs. Pinned by sha256; the tarball is cached in /tmp so
# re-runs don't re-download. FALLBACK (download/verify failure): ldd-copy the
# host ffmpeg with the same launcher mechanism as mkvpropedit above.
FFSTATIC_VERSION="7.0.2"
# Per-arch static builds from the same source (F18): both sha256-pinned.
case "$ARCH" in
    aarch64) FFSTATIC_ARCHNAME="arm64"
             FFSTATIC_SHA256="f4149bb2b0784e30e99bdda85471c9b5930d3402014e934a5098b41d0f7201b1" ;;
    *)       FFSTATIC_ARCHNAME="amd64"
             FFSTATIC_SHA256="abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67" ;;
esac
FFSTATIC_URLS="https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${FFSTATIC_ARCHNAME}-static.tar.xz
https://johnvansickle.com/ffmpeg/old-releases/ffmpeg-${FFSTATIC_VERSION}-${FFSTATIC_ARCHNAME}-static.tar.xz"
FFSTATIC_TAR="/tmp/amm-ffmpeg-${FFSTATIC_VERSION}-${FFSTATIC_ARCHNAME}-static.tar.xz"
FF_MARKER="$TOOLS_DIR/.amm-ffmpeg"
WANT_FF="${FFSTATIC_VERSION}-${FFSTATIC_ARCHNAME}"

ffmpeg_static_ok=""
if [ -x "$TOOLS_DIR/bin/ffmpeg" ] && [ "$(cat "$FF_MARKER" 2>/dev/null)" = "$WANT_FF" ]; then
    echo "✓ Static ffmpeg ${WANT_FF} already bundled"
    ffmpeg_static_ok=1
else
    for url in $FFSTATIC_URLS; do
        if [ ! -f "$FFSTATIC_TAR" ] || ! echo "${FFSTATIC_SHA256}  ${FFSTATIC_TAR}" | sha256sum -c --quiet - 2>/dev/null; then
            echo "→ Downloading static ffmpeg ${FFSTATIC_VERSION} (~40 MB) from ${url%%/releases*}… "
            curl -fL --progress-bar -o "$FFSTATIC_TAR" "$url" || { rm -f "$FFSTATIC_TAR"; continue; }
        fi
        if echo "${FFSTATIC_SHA256}  ${FFSTATIC_TAR}" | sha256sum -c --quiet - 2>/dev/null; then
            echo "→ Extracting static ffmpeg + ffprobe (sha256 verified) ..."
            TMP_FF="$(mktemp -d)"
            tar -xJf "$FFSTATIC_TAR" -C "$TMP_FF" --strip-components=1 \
                "ffmpeg-${FFSTATIC_VERSION}-${FFSTATIC_ARCHNAME}-static/ffmpeg" \
                "ffmpeg-${FFSTATIC_VERSION}-${FFSTATIC_ARCHNAME}-static/ffprobe"
            install -m 755 "$TMP_FF/ffmpeg" "$TOOLS_DIR/bin/ffmpeg"
            install -m 755 "$TMP_FF/ffprobe" "$TOOLS_DIR/bin/ffprobe"
            rm -rf "$TMP_FF"
            echo "$WANT_FF" > "$FF_MARKER"
            echo "✓ Static ffmpeg + ffprobe ${WANT_FF} bundled"
            ffmpeg_static_ok=1
            break
        else
            echo "⚠ sha256 mismatch for ${url} — trying next source"
            rm -f "$FFSTATIC_TAR"
        fi
    done
fi

if [ -z "$ffmpeg_static_ok" ] && [ -n "$CROSS" ]; then
    echo "✗ Cross-staging ${ARCH}: static ffmpeg is REQUIRED (no ldd fallback cross-arch)."
    exit 1
fi
if [ -z "$ffmpeg_static_ok" ]; then
    echo "⚠ Static ffmpeg unavailable — falling back to ldd-copying the host build."
    FF_EXCLUDE='^(ld-linux.*|libc|libm|libdl|libpthread|librt|libresolv|libutil)\.so'
    for FTOOL in ffmpeg ffprobe; do
        FT_SRC="$(command -v "$FTOOL" || true)"
        if [ -n "$FT_SRC" ]; then
            echo "→ Bundling ${FTOOL} from ${FT_SRC} ..."
            cp -L "$FT_SRC" "$TOOLS_DIR/bin/${FTOOL}.bin"
            chmod +x "$TOOLS_DIR/bin/${FTOOL}.bin"
            ldd "$FT_SRC" | awk '/=> \// {print $3}' | sort -u | while read -r lib; do
                base="$(basename "$lib")"
                echo "$base" | grep -qE "$FF_EXCLUDE" && continue
                [ -e "$TOOLS_DIR/lib/$base" ] || cp -L "$lib" "$TOOLS_DIR/lib/" 2>/dev/null || true
            done
            cat > "$TOOLS_DIR/bin/${FTOOL}" <<'WRAP'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
export LD_LIBRARY_PATH="$HERE/../lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$HERE/$(basename "$0").bin" "$@"
WRAP
            chmod +x "$TOOLS_DIR/bin/${FTOOL}"
            echo "✓ ${FTOOL} bundled (ldd)"
        else
            echo "⚠ ${FTOOL} not found on build host — the AppImage will rely on the"
            echo "  host's ${FTOOL} (if any); remux/probe features degrade without it."
        fi
    done
fi
echo "  bundled-tools total: $(du -sh "$TOOLS_DIR" | awk '{print $1}')"

# ── Summary ───────────────────────────────────────────────────────────────────
# Cross-staged binaries can't execute on the build host — report the marker.
if [ -n "$CROSS" ]; then
    PY_ACTUAL="Python $(cat "$PY_MARKER" 2>/dev/null || echo "${PY_VERSION}-${ARCH}")"
else
    PY_ACTUAL=$("${PYTHON_DIR}/bin/python3" --version)
fi
echo ""
echo "Build assets ready:"
echo "  Python: ${PY_ACTUAL}  →  ${PYTHON_DIR}/"
echo "  Packages ($(du -sh "${PACKAGES_DIR}" | awk '{print $1}')) →  ${PACKAGES_DIR}/"
echo ""
if [ -n "$CROSS" ]; then
    echo "Now run: npm run build:arm64   (assets staged for ${ARCH})"
else
    echo "Now run: npm run build"
fi
