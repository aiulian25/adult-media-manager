"""
Adult Media Manager - FastAPI backend.
Serves web UI and provides API endpoints for scanning, matching, and renaming adult content.
"""

import os
import re
import json
import copy
import asyncio
import threading
import subprocess
import tempfile
import base64
import hashlib
import shutil
from pathlib import Path
from typing import Optional

import httpx

import time as _time
import uuid

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, field_validator

from app.core.detector import (
    detect, MediaType, is_video_file, is_subtitle_file,
    VIDEO_EXTENSIONS, SUBTITLE_EXTENSIONS,
)
from app.core.matcher import (
    score_match, find_best_match, match_site, normalize as _perf_normalize,
    _performer_pair_score, PARTIAL_NAME_SCORE as _PARTIAL_NAME_SCORE,
)
from app.core.formatter import (
    apply_template, build_new_path, TEMPLATES, TEMPLATE_VARS, extract_template_vars,
)
from app.core.renamer import (
    execute_rename, execute_rename_with_companions, resolve_collision,
    RenameAction, RenameResult,
)
from app.core.history import RenameHistory, HistoryEntry
from app.core.catalog import Catalog
from app.core.tools import ffmpeg_path, ffprobe_path
from app.core.jobs import JobStore
from app.core.embedder import (
    validate_embed_mode, ffmpeg_metadata_args, build_mkv_tags_xml, plan_embed,
)
from app.api.tpdb import TPDBClient
from app.api.stashdb import (
    StashDBClient, compute_oshash, compute_phash_ffmpeg, compute_phash_ffmpeg_sync,
    PHASH_MATCH_TIMEOUT, _classify_error as _classify_stashdb_error,
)


def _resolve_app_version() -> str:
    """Resolve the app version from ONE source, so no stale literal can drift.

    Priority:
      1. ``AMM_VERSION`` env — Docker sets it from the Dockerfile ``ARG`` (which
         also drives the image ``LABEL``); the Electron launcher sets it from
         ``package.json`` via ``app.getVersion()``.
      2. ``package.json`` on disk — dev runs from the repo root, and the packaged
         deb/AppImage ship it at ``resources/app/package.json``. (It is NOT copied
         into the Docker image, which is exactly why Docker uses the env above.)
      3. ``"0.0.0"`` — an honest "unknown", never a stale hard-coded release number.
    """
    env = os.getenv("AMM_VERSION", "").strip()
    if env:
        return env
    for cand in (
        Path(__file__).resolve().parent.parent / "package.json",
        Path.cwd() / "package.json",
    ):
        try:
            v = json.loads(cand.read_text(encoding="utf-8")).get("version")
            if v:
                return str(v)
        except Exception:
            continue
    return "0.0.0"


APP_VERSION = _resolve_app_version()

app = FastAPI(
    title="Adult Media Manager",
    version=APP_VERSION,
    description="Professional metadata organizer for adult content"
)

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Without an explicit Cache-Control, Chromium applies *heuristic* freshness
# (10% of the file's age) to /static responses and can keep serving JS/CSS
# from its disk cache for hours after a package upgrade replaced the files —
# the app updates but the UI doesn't. `no-cache` still allows caching but
# forces an ETag revalidation on every load; against the local/LAN server a
# 304 costs ~1 ms, so the UI is always the installed version.
@app.middleware("http")
async def _no_stale_ui_cache(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# Initialize history tracking
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
history = RenameHistory(DATA_DIR / "history.json")

# File→match catalog (review item R1): SQLite, in the same secured DATA_DIR. An
# additive store that tracks what AMM has seen/organised so re-scans are
# incremental (skip already-organised files) and duplicate content is detectable.
# Best-effort — if it can't initialise it self-disables and the app still works.
catalog = Catalog(DATA_DIR / "catalog.db")

# ── FFmpeg staging directory ─────────────────────────────────────────────────
# Work files are written here (local Docker named volume, /data) rather than
# on the NAS/FUSE mount.  Advantages:
#   • FFmpeg reads the original once and writes at local-disk speed.
#   • No partial or ghost files ever appear in the user's media library.
#   • The original is only atomically replaced in the final os.replace() step.
#   • A container crash leaves stale files here, not in the library;
#     they are purged on every startup (see loop below).
_EMBED_STAGING_DIR: Path = DATA_DIR / "embed-tmp"
_EMBED_STAGING_DIR.mkdir(parents=True, exist_ok=True)
for _f in list(_EMBED_STAGING_DIR.iterdir()):  # purge crash leftovers
    try:
        _f.unlink(missing_ok=True)
    except OSError:
        pass

# In-memory store for Phase-2 (embed) background jobs.
# Keyed by job_id (hex uuid4). Each value:
#   {"total": int, "done": int, "warnings": list[dict], "complete": bool, "created": float}
# Cleaned up automatically when older than EMBED_JOB_TTL seconds.
# PROCESS-LOCAL: shared with _match_sessions; requires single-worker deployment
# (enforced by the launchers + _startup_single_worker_check). See review item P7.
#
# DURABILITY (review item R2): the in-memory dict stays the hot path while the
# process lives, but every create/progress/finish is written through to a SQLite
# JobStore so a page refresh can re-attach to an in-flight job and a server
# restart returns the last-known state (interrupted) instead of a 404. The store
# lives in the same secured DATA_DIR as history/catalog.
_embed_jobs: dict[str, dict] = {}
EMBED_JOB_TTL = 600  # 10 minutes
_job_store = JobStore(DATA_DIR / "jobs.db")


def _job_create(job_id: str, total: int, kind: str = "embed") -> dict:
    """Register a background embed job in memory AND in the durable store."""
    complete = total == 0  # trivially complete if there's nothing to embed
    job = {
        "total": total, "done": 0, "warnings": [],
        "complete": complete, "created": _time.monotonic(),
    }
    _embed_jobs[job_id] = job
    _job_store.create(job_id, kind, total, complete=complete)
    return job


def _job_progress(job_id: str, warning: Optional[dict] = None) -> None:
    """Record one unit of progress (and an optional warning) for a job."""
    job = _embed_jobs.get(job_id)
    if job is None:
        return
    job["done"] += 1
    if warning:
        job["warnings"].append(warning)
    _job_store.progress(job_id, job["done"], job["warnings"])


def _job_finish(job_id: str) -> None:
    """Mark a job complete in memory AND in the durable store."""
    job = _embed_jobs.get(job_id)
    if job is not None:
        job["complete"] = True
    _job_store.finish(job_id, "complete")

# ── Persistent user settings (API keys saved via the UI) ──────────────────────
# Keys set through environment variables ALWAYS take precedence.
# The settings file is a fallback for users who did not configure their .env /
# docker-compose environment.  Key values are stored in plain text inside the
# already-secured Docker named volume (/data) — the same place history is kept.
# They are NEVER returned to the browser; only an "active / source" status is.
_SETTINGS_FILE: Path = DATA_DIR / "settings.json"
_SETTINGS_KEY_MAX_LEN = 512   # sanity cap — real keys are well under this

# UI preferences persisted alongside the API keys.  These are NOT secrets, so
# (unlike keys) they are safely returned to the browser.  Both are whitelisted
# on write AND read because `locale` is interpolated into the static path
# `/static/locales/<locale>.json` on the client — an unvalidated value would be
# a path-injection vector.  Keep these in sync with the locale files that ship
# under app/static/locales/ and the <option>s in the Settings UI.
_ALLOWED_LOCALES: frozenset[str] = frozenset({"en", "de", "es", "fr", "ja", "pt"})
_ALLOWED_THEMES:  frozenset[str] = frozenset({"default", "dark", "light"})
_DEFAULT_LOCALE = "en"
_DEFAULT_THEME  = "default"
# Persisted default metadata write mode (embedder.EMBED_MODES). Not a secret, so
# it is returned to the browser like locale/theme; the client uses it to seed the
# per-rename Metadata selector and sends it back on rename/save. "smart" =
# "Both (file + .nfo)", the recommended default.
_DEFAULT_EMBED_MODE = "embed"   # full ffmpeg remux + NFO — the universal path


def _effective_locale() -> str:
    val = _load_settings().get("locale")
    return val if val in _ALLOWED_LOCALES else _DEFAULT_LOCALE


def _effective_theme() -> str:
    val = _load_settings().get("theme")
    return val if val in _ALLOWED_THEMES else _DEFAULT_THEME


def _effective_embed_mode() -> str:
    from app.core.embedder import EMBED_MODES
    val = _load_settings().get("embed_mode")
    return val if val in EMBED_MODES else _DEFAULT_EMBED_MODE


# Performer order in generated names/NFOs: "female_first" (default) sorts
# female performers to the front of every scene's performer list at match
# time; "source" keeps whatever order TPDB/StashDB returned.
_ALLOWED_PERFORMER_ORDERS = {"female_first", "source"}
_DEFAULT_PERFORMER_ORDER = "female_first"


def _effective_performer_order() -> str:
    val = _load_settings().get("performer_order")
    return val if val in _ALLOWED_PERFORMER_ORDERS else _DEFAULT_PERFORMER_ORDER


# Genders counted as "female" for the ♀-first sort. Sources use different
# vocabularies (StashDB enum FEMALE/TRANSGENDER_FEMALE, TPDB "Female"/"Trans");
# values arrive normalized to lowercase by the API clients.
_FEMALE_GENDERS = {"female", "transgender_female", "trans female", "trans_female"}


def _apply_performer_order(scene: dict) -> dict:
    """Reorder a scene dict's performers ♀-first (when the setting says so).

    The sort is STABLE and keys only on is-female — performers whose gender the
    source didn't state keep their relative position instead of being guessed.
    `performer_genders` is re-aligned in the same pass so the UI badges stay
    truthful. Everything downstream ({performer}/{performers}, match cards,
    NFO <actor> order) reads the list, so sorting once here keeps them agreed.
    """
    if _effective_performer_order() != "female_first":
        return scene
    perfs = scene.get("performers") or []
    if len(perfs) < 2:
        return scene
    genders = list(scene.get("performer_genders") or [])
    genders += [None] * (len(perfs) - len(genders))
    order = sorted(range(len(perfs)),
                   key=lambda i: 0 if (genders[i] or "") in _FEMALE_GENDERS else 1)
    if order != list(range(len(perfs))):
        scene["performers"] = [perfs[i] for i in order]
        scene["performer_genders"] = [genders[i] for i in order]
    return scene


def _load_settings() -> dict:
    """Return the persisted settings dict, or {} if missing / corrupt."""
    try:
        raw = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    """Atomically write settings to disk (temp-file swap)."""
    tmp = _SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(_SETTINGS_FILE))


def _effective_key(env_var: str, settings_key: str) -> tuple[str | None, str | None]:
    """
    Return (key_value, source) where source is 'env', 'settings', or None.
    Environment variables always win over saved settings.
    """
    env_val = os.getenv(env_var, "").strip()
    if env_val:
        return env_val, "env"
    saved = _load_settings().get(settings_key, "").strip()
    if saved:
        return saved, "settings"
    return None, None


def _init_tpdb() -> "TPDBClient | None":
    key, _ = _effective_key("TPDB_API_KEY", "tpdb_api_key")
    if not key:
        print("WARNING: TPDB API key not configured. Set TPDB_API_KEY or use the Settings UI.")
        return None
    try:
        return TPDBClient(api_key=key)
    except Exception as e:
        print(f"WARNING: TPDB client init failed: {e}")
        return None


def _init_stashdb() -> "StashDBClient | None":
    key, _ = _effective_key("STASHDB_API_KEY", "stashdb_api_key")
    if not key:
        print("INFO: StashDB API key not configured. Set STASHDB_API_KEY or use the Settings UI.")
        return None
    try:
        return StashDBClient(api_key=key)
    except Exception as e:
        print(f"WARNING: StashDB client init failed: {e}")
        return None


# Initialize API clients (env var → saved settings → None)
tpdb    = _init_tpdb()
stashdb = _init_stashdb()

# When running as a native desktop app (DEB/AppImage), the Electron main process
# sets AMM_NATIVE=1. In this mode the Docker-centric path allowlist is lifted so
# users can browse and scan any directory on their own machine. Docker deployments
# keep the restriction to prevent host-filesystem exposure.
_AMM_NATIVE: bool = os.getenv("AMM_NATIVE", "0") == "1"

# Allowed roots for filesystem access — never include "/" (root of the whole filesystem).
# Only used in Docker mode. Native mode bypasses this list entirely.
_default_roots: set[Path] = {
    Path("/media"),
    Path("/mnt"),
    Path("/data"),
    Path("/downloads"),
    Path("/organized"),
    Path("/home"),       # User home directories (DEB/AppImage native installs)
    Path("/root"),       # Root user home
    Path("/srv"),        # NAS/network mounts
    Path("/nas"),        # NAS mount point
    Path("/storage"),    # Additional storage mount point
    Path("/run/media"),  # Removable media (systemd automount)
}
_extra_roots: set[Path] = set()
for _r in os.getenv("AMM_EXTRA_ROOTS", "").split(":"):
    _r = _r.strip()
    if _r and _r != "/":           # safety: never allow bare root
        _extra_roots.add(Path(_r))
ALLOWED_ROOTS: frozenset[Path] = frozenset(_default_roots | _extra_roots)


def _is_allowed_path(p: Path) -> bool:
    """Return True only when *p* is inside one of the ALLOWED_ROOTS.

    Native mode (AMM_NATIVE=1) skips the allowlist — the user is browsing
    their own machine, so any absolute path is valid.
    """
    if _AMM_NATIVE:
        return p.resolve().is_absolute()
    rp = p.resolve()
    return any(
        rp == root or str(rp).startswith(str(root) + "/")
        for root in ALLOWED_ROOTS
    )


# ── Update notifier (F17) ────────────────────────────────────────────────────
# One shared implementation for every target. The ONLY per-target inputs are:
# The backend only PUBLISHES release facts (latest version + asset list with
# sha256 digests from the GitHub API); downloading and installing live in the
# Electron main process, so the HTTP API carries no write endpoint for updates
# (a LAN-exposed Docker port can never be induced to fetch binaries).
# AMM_UPDATE_CHECK=0 disables ALL outbound update traffic (zero-egress option).
_AMM_UPDATE_CHECK: bool = os.getenv("AMM_UPDATE_CHECK", "1") != "0"
_GITHUB_REPO = "aiulian25/adult-media-manager"
_RELEASES_URL = f"https://github.com/{_GITHUB_REPO}/releases"
_UPDATE_TTL_OK = 24 * 3600      # successful check → re-ask GitHub once a day
_UPDATE_TTL_FAIL = 3600         # failed check → retry hourly, don't hammer
_update_cache: dict = {"checked_at": 0.0, "release": None, "ok": False}
_update_lock = asyncio.Lock()


def _version_tuple(v: str) -> tuple:
    """'v1.4.1' → (1, 4, 1); non-numeric pieces count as 0."""
    parts = []
    for piece in v.strip().lstrip("vV").split("."):
        m = re.match(r"\d+", piece)
        parts.append(int(m.group()) if m else 0)
    return tuple(parts)


async def _fetch_latest_release() -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{_GITHUB_REPO}/releases/latest",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": f"adult-media-manager/{APP_VERSION}",
                },
            )
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:
        return None  # offline / rate-limited / DNS-blocked → silently no update


async def _get_update_info() -> Optional[dict]:
    """Newer-release info or None. Cached in memory; failures are silent."""
    if not _AMM_UPDATE_CHECK:
        return None
    now = _time.time()
    async with _update_lock:
        ttl = _UPDATE_TTL_OK if _update_cache["ok"] else _UPDATE_TTL_FAIL
        if not _update_cache["checked_at"] or now - _update_cache["checked_at"] >= ttl:
            release = await _fetch_latest_release()
            _update_cache.update(checked_at=now, release=release, ok=release is not None)
        release = _update_cache["release"]
    if not release:
        return None
    latest = str(release.get("tag_name", "")).lstrip("vV")
    if not latest or _version_tuple(latest) <= _version_tuple(APP_VERSION):
        return None
    # Full asset list so the desktop shell picks the right package (type+arch)
    # itself; size + sha256 digest let its downloader verify integrity
    # end-to-end (during download AND re-checked right before install).
    return {
        "latest": latest,
        "url": str(release.get("html_url") or _RELEASES_URL),
        "assets": [
            {
                "name": str(a["name"]),
                "url": str(a["browser_download_url"]),
                "size": int(a.get("size") or 0),
                "digest": a.get("digest"),
            }
            for a in (release.get("assets") or [])
            if a.get("name")
            and str(a.get("browser_download_url", "")).startswith("https://")
        ],
    }


@app.on_event("startup")
async def _startup_single_worker_check():
    """
    AMM keeps match sessions (_match_sessions), embed-job progress
    (_embed_jobs) and the embed concurrency semaphore (_embed_sem) in
    PROCESS-LOCAL memory.  None of it can be shared across worker processes, so
    the app MUST run with a single worker: with >1 worker the SSE match stream
    and the embed-status poller would reach a different process than the one
    holding the state and fail (session-not-found / progress stuck).

    The shipped launchers already guarantee one worker (the Docker entrypoint
    passes ``--workers 1``; the Electron deb/AppImage spawns uvicorn with the
    default single worker).  This is a best-effort safety net for custom
    deployments: warn loudly when a conventional multi-worker env knob is set
    above 1.  ``AMM_ALLOW_MULTIWORKER=1`` silences the warning (the flows still
    won't work — that needs the shared-store refactor, review item R2).

    Identical behaviour on every build target — pure env inspection, no
    platform-specific code.
    """
    # Durable jobs (R2): any embed job still marked "running" belongs to a
    # previous process whose FFmpeg work died with it — flip it to "interrupted"
    # so a re-attaching client sees a terminal state, and prune old terminal jobs.
    try:
        interrupted = _job_store.interrupt_running()
        if interrupted:
            print(f"INFO: marked {interrupted} interrupted embed job(s) from a previous run.")
        _job_store.prune(EMBED_JOB_TTL)
    except Exception as e:
        print(f"WARNING: job store startup maintenance failed: {e}")

    # Thumbnail garbage collection (roadmap-2 F1): extract-thumbnails writes six
    # JPEGs per Manual-Edit open and nothing ever deleted them. Age-prune dirs
    # older than 7 days — EXCEPT those containing selected.jpg, which confirmed
    # cache entries reference via their stored thumbnail_url (deleting them would
    # silently break match-row previews). Best-effort; never blocks startup.
    try:
        cutoff = _time.time() - 7 * 86400
        thumbs_root = DATA_DIR / "thumbnails"
        removed = 0
        if thumbs_root.is_dir():
            for d in thumbs_root.iterdir():
                try:
                    if (d.is_dir() and d.stat().st_mtime < cutoff
                            and not (d / "selected.jpg").exists()):
                        shutil.rmtree(d, ignore_errors=True)
                        removed += 1
                except OSError:
                    continue
        if removed:
            print(f"INFO: pruned {removed} stale thumbnail dir(s).")
    except Exception as e:
        print(f"WARNING: thumbnail prune failed: {e}")

    if os.getenv("AMM_ALLOW_MULTIWORKER", "0") == "1":
        return
    for var in ("AMM_WORKERS", "WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS"):
        raw = os.getenv(var, "").strip()
        if not raw:
            continue
        try:
            n = int(raw)
        except ValueError:
            continue
        if n > 1:
            print(
                f"WARNING: {var}={raw} requests multiple workers, but Adult Media "
                "Manager stores match sessions and embed-job progress in "
                "process-local memory. Matching (SSE) and embed-progress polling "
                "WILL break across workers. Run a single worker (the shipped "
                "default), or set AMM_ALLOW_MULTIWORKER=1 to acknowledge."
            )
            break
    print(f"INFO: Adult Media Manager started (single-worker mode, pid={os.getpid()}).")


@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown."""
    if tpdb:
        await tpdb.close()
    if stashdb:
        await stashdb.close()


@app.get("/")
async def index():
    """Serve the main web UI."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/Adult%20Media%20Manager.png")
@app.get("/Adult Media Manager.png")
async def logo():
    """Serve the round app logo (kept for backward compat with cached favicons).

    The canonical asset is /static/icon.png; this legacy path just serves the same
    round PNG so any old reference still gets the current logo, not the retired
    placeholder SVG. Falls back to a tiny round SVG if the file is somehow missing.
    """
    icon = STATIC_DIR / "icon.png"
    if icon.is_file():
        return FileResponse(str(icon), media_type="image/png")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<circle cx="32" cy="32" r="32" fill="#14100c"/>'
        '<circle cx="32" cy="32" r="20" fill="none" stroke="#b24bf3" stroke-width="4"/>'
        '</svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


# ─── Models ────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    path: str
    recursive: bool = True
    # Incremental rescan (review item R1): when true, files the catalog already
    # records as organised by AMM are excluded from the results, so a re-scan of a
    # large library only surfaces new/unorganised work. Default false → unchanged
    # behaviour.
    skip_organized: bool = False
    # F12: scan dot-files/dirs too. Default False → today's behaviour (hidden
    # media is skipped and tallied); the picker can already SHOW hidden files,
    # so the scanner must be able to honour a hidden selection.
    include_hidden: bool = False

    @field_validator("path")
    @classmethod
    def validate_path(cls, v: str) -> str:
        # Comma-separated file list — validate each part individually
        if ',' in v:
            parts = [p.strip() for p in v.split(',') if p.strip()]
            missing = [p for p in parts if not Path(p).expanduser().exists()]
            if missing and len(missing) == len(parts):
                raise ValueError(f"None of the provided paths exist")
            return v
        p = Path(v).expanduser().resolve()
        if not p.exists():
            raise ValueError(f"Path does not exist: {v}")
        return str(p)


class MatchRequest(BaseModel):
    files: list[dict]
    datasource: str = "tpdb"
    template: Optional[str] = None
    auto_match: bool = True
    # When True, ignore the persistent match cache and re-query the API (D3).
    # The fresh results still update the cache.
    refresh: bool = False


# Metadata write modes (Phase 2 of a rename, and manual save). The mode names are
# the stable API/UI contract; the actual per-container strategy is decided by the
# pluggable planner in app/core/embedder.py (review item R4):
#   "embed"    – FFmpeg remux for every container AND an NFO sidecar (default;
#                preserves historical behaviour).
#   "smart"    – fast IN-PLACE tagging where the container supports it
#                (mkvpropedit for Matroska, AtomicParsley for MP4/M4V/MOV);
#                every other container, or a missing/failed in-place tool, falls
#                back to the FFmpeg remux, so the outcome always matches "embed".
#   "nfo_only" – write only the NFO sidecar; skip embedding entirely.
#                Jellyfin/Plex read the sidecar, so metadata still shows, but we
#                avoid the heavy read+local-write+NAS-copy-back per file.
# (EMBED_MODES / validate_embed_mode now live in app/core/embedder.py.)
_validate_embed_mode = validate_embed_mode


# In-place tagging tools. Each ships the same way across targets so behaviour is
# consistent; if a tool is missing the planner falls back to the FFmpeg remux:
#   • Docker   — installed in the image (Dockerfile apt-get).
#   • deb      — declared in package.json deb.depends.
#   • AppImage — bundled by prepare-build.sh into bundled-tools/ and pointed at via
#                AMM_MKVPROPEDIT / AMM_ATOMICPARSLEY (set by electron/main.js);
#                if the bundle is absent it resolves on PATH, then remuxes.


def _mkvpropedit_path() -> Optional[str]:
    """Resolve the mkvpropedit binary (Matroska in-place tagging).

    AMM_MKVPROPEDIT lets a packager point at a bundled binary; otherwise we look
    it up on PATH.  Returns None when unavailable so callers can fall back.
    """
    override = os.getenv("AMM_MKVPROPEDIT", "").strip()
    if override:
        return override if Path(override).exists() else None
    return shutil.which("mkvpropedit")


def _atomicparsley_path() -> Optional[str]:
    """Resolve the AtomicParsley binary (MP4/M4V/MOV in-place tagging).

    AMM_ATOMICPARSLEY lets a packager point at a bundled binary; otherwise we look
    it up on PATH (the Debian package name is ``atomicparsley`` but the binary is
    ``AtomicParsley`` — accept either casing). Returns None so callers fall back.
    """
    override = os.getenv("AMM_ATOMICPARSLEY", "").strip()
    if override:
        return override if Path(override).exists() else None
    return shutil.which("AtomicParsley") or shutil.which("atomicparsley")


class RenameRequest(BaseModel):
    operations: list[dict]
    action: str = "test"
    embed_mode: str = "embed"
    # Collision policy (F1): what to do when a target path already exists (on
    # disk, or claimed earlier in this batch). suffix = auto-number " (N)".
    on_conflict: str = "suffix"

    @field_validator("on_conflict")
    @classmethod
    def validate_on_conflict(cls, v: str) -> str:
        if v not in ("suffix", "skip", "fail"):
            raise ValueError("on_conflict must be 'suffix', 'skip' or 'fail'")
        return v

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        valid = {a.value for a in RenameAction}
        if v not in valid:
            raise ValueError(f"Invalid action: {v}. Must be one of: {valid}")
        return v

    @field_validator("embed_mode")
    @classmethod
    def validate_embed_mode(cls, v: str) -> str:
        return _validate_embed_mode(v)


class ManualMetadataRequest(BaseModel):
    file_path: str
    title: str
    site: Optional[str] = None
    performers: list[str] = []
    release_date: Optional[str] = None
    tags: list[str] = []
    quality: Optional[str] = None
    thumbnail_index: Optional[int] = None  # Which generated thumbnail to use
    embed_mode: str = "embed"             # embed | smart | nfo_only (embedder.EMBED_MODES)
    # Synopsis + provider identity (F7) — set by the Fetch buttons; absent for
    # hand-typed entries, which then keep id="manual" (no <uniqueid> in the NFO).
    description: Optional[str] = None
    scene_id: Optional[str] = None
    source: Optional[str] = None

    @field_validator("embed_mode")
    @classmethod
    def validate_embed_mode(cls, v: str) -> str:
        return _validate_embed_mode(v)

    @field_validator("source")
    @classmethod
    def validate_source(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip().lower() or None
        if v is not None and v not in ("tpdb", "stashdb"):
            raise ValueError("source must be 'tpdb' or 'stashdb'")
        return v

    @field_validator("scene_id")
    @classmethod
    def validate_scene_id(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip() or None
        if v is not None and len(v) > 200:
            raise ValueError("scene_id too long")
        return v


def _parse_nfo(nfo_path: Path) -> dict | None:
    """
    Parse a Jellyfin-compatible NFO sidecar written by write_nfo().
    Returns a metadata dict on success, None if the file is missing/malformed.
    Intentionally never raises — a bad NFO must never break scanning.
    """
    import xml.etree.ElementTree as ET
    try:
        tree = ET.parse(nfo_path)
        root = tree.getroot()
        def _text(tag: str) -> str:
            el = root.find(tag)
            return (el.text or "").strip() if el is not None else ""

        performers = [
            (a.findtext("name") or "").strip()
            for a in root.findall("actor")
            if (a.findtext("name") or "").strip()
        ]
        tags = [
            (t.text or "").strip()
            for t in root.findall("tag")
            if (t.text or "").strip()
        ]
        # Provider linkage (F7): read the <uniqueid> back so an organised file
        # keeps its scene id/source across rescans instead of losing them the
        # moment the only copy lives in the sidecar.
        uid = root.find("uniqueid")
        return {
            "title":        _text("title"),
            "site":         _text("studio"),
            "performers":   performers,
            "release_date": _text("premiered") or _text("releasedate"),
            "tags":         tags,
            "description":  _text("plot"),
            "scene_id":     (uid.text or "").strip() if uid is not None else "",
            "source":       (uid.get("type") or "").strip() if uid is not None else "",
        }
    except Exception:
        return None


# ─── Scan Endpoint ─────────────────────────────────────────────────────

# Probe each scanned video's duration via ffprobe so the matcher can use runtime
# as a disambiguator (review item D1). On by default; set AMM_SCAN_PROBE_DURATION=0
# to skip it on very large libraries / slow mounts where the extra per-file
# ffprobe is not worth the scan-time cost. Purely a performance knob — matching
# degrades gracefully to the previous behaviour when duration is absent. This is
# a config/env concern, so it lives here rather than in core scoring logic, and
# behaves identically on Docker/deb/AppImage.
_SCAN_PROBE_DURATION: bool = os.getenv("AMM_SCAN_PROBE_DURATION", "1") == "1"

# Compute a perceptual hash (pHash) for each scanned video and store it in the
# catalog so duplicate detection can catch *re-encodes* — the same scene at a
# different bitrate/resolution/container has a DIFFERENT oshash (exact bytes) but a
# near-identical pHash. OFF by default (unlike duration): pHash needs an ffmpeg
# frame-decode per file — materially heavier than the header-only ffprobe — so it
# is opt-in for users who want near-duplicate grouping. Set AMM_SCAN_PHASH=1 to
# enable. Purely a performance/opt-in knob (mirrors AMM_SCAN_PROBE_DURATION); when
# off, no ffmpeg runs at scan time and the phash column stays NULL — zero
# regression. Behaves identically on Docker/deb/AppImage (same ffmpeg dependency).
_SCAN_PHASH: bool = os.getenv("AMM_SCAN_PHASH", "0") == "1"

# When renaming an API-matched scene, download the scene's poster image and save
# it next to the video as "<stem>-poster.jpg" (referenced by the NFO) so
# Jellyfin/Plex show it. ON by default; set AMM_FETCH_POSTERS=0 to disable the
# server-side fetch (e.g. for zero-egress deployments — the browser already loads
# these same CDN images during matching, so this adds no new trust boundary, but
# some users want the server itself to make no outbound image requests). The
# manual-edit poster path never touches the network — it copies the locally
# generated thumbnail — so it is unaffected by this flag.
_FETCH_POSTERS: bool = os.getenv("AMM_FETCH_POSTERS", "1") == "1"


def _probe_duration_seconds(path: Path) -> Optional[float]:
    """Return media duration in seconds via ffprobe, or None on any failure.

    Best-effort and never raises: a missing ffprobe, an unreadable/corrupt file,
    or a slow mount simply yields None and duration-based scoring is skipped for
    that file. Reads only the container header (format=duration), so it does not
    scan the whole file. ffprobe is provisioned identically across build targets
    (same binary used for thumbnails/phash), so behaviour does not diverge.
    """
    try:
        out = subprocess.run(
            [ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=8,
        )
        val = float(out.stdout.strip())
        return val if val > 0 else None
    except Exception:
        return None


def _resolve_scan_paths_req(req: "ScanRequest") -> tuple[list[Path], Optional[str]]:
    """Resolve a scan request into a candidate file list, honouring ``recursive``.

    Returns (paths, error). ``error`` is non-None only for the directory access /
    not-found cases the UI surfaces distinctly. Shared by the batch ``/api/scan``
    and the streaming ``/api/scan-stream`` so the two never diverge in what they
    enumerate.
    """
    if ',' in req.path:
        # A comma-separated list may mix files AND directories — the native OS
        # picker (deb/AppImage) returns multiple folders, multiple files, or a
        # mix. Expand each directory into its media candidates (honouring
        # ``recursive``) instead of dropping it; a bare directory entry would
        # otherwise be skipped by _build_file_entry's is_file() check.
        out: list[Path] = []
        for raw in req.path.split(','):
            p = Path(raw.strip())
            if not raw.strip() or not p.exists():
                continue
            if p.is_dir():
                try:
                    out.extend(sorted(p.rglob("*")) if req.recursive else sorted(p.iterdir()))
                except (OSError, PermissionError):
                    continue
            else:
                out.append(p)
        return out, None
    base = Path(req.path)
    if base.is_file():
        return [base], None
    if base.is_dir():
        try:
            paths = sorted(base.rglob("*")) if req.recursive else sorted(base.iterdir())
        except (OSError, PermissionError) as e:
            return [], f"Cannot access directory: {e}"
        return paths, None
    return [], f"Path not found: {req.path}"


_SCAN_MEDIA_EXTS = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS


def _known_site_norms() -> set:
    """Normalized known-site names for folder-context detection (F8).

    Computed ONCE per scan by the callers and threaded into every
    `_build_file_entry` call — never per file (the store read is cached, but
    the set comprehension over hundreds of sites is not free ×10k files).
    """
    return {_perf_normalize(s.get("name") or "")
            for s in _load_known_sites() if s.get("name")}


def _build_file_entry(p: Path, known_sites: Optional[set] = None,
                      include_hidden: bool = False) -> Optional[dict]:
    """Build the scan ``file_entry`` for one path, or None to skip it.

    All blocking filesystem/CPU work for a single file (existence/stat, NFO XML
    parse, regex detection, oshash, optional ffprobe) lives here so it can be run
    in a worker thread by the streaming endpoint and reused verbatim by the batch
    endpoint — one implementation, identical across Docker/deb/AppImage.
    """
    # Double-check file still exists (race condition with deletion)
    try:
        if not p.is_file():
            return None
    except (OSError, PermissionError):
        return None

    # Skip hidden files: .fuse_hidden* stubs, .DS_Store, .Trash, etc. — unless
    # the scan explicitly asked for them (F12); dot-junk without a media
    # extension still falls out at the next check either way.
    if p.name.startswith('.') and not include_hidden:
        return None
    if p.suffix.lower() not in _SCAN_MEDIA_EXTS:
        return None

    try:
        det = detect(p, known_sites=known_sites)
    except Exception as e:
        print(f"WARNING: Could not detect {p}: {e}")
        return None

    # NFO sidecar detection (read-only) — presence means "already organised".
    nfo_path = p.with_suffix(".nfo")
    nfo_meta = _parse_nfo(nfo_path) if nfo_path.is_file() else None

    try:
        file_size = p.stat().st_size
    except (OSError, PermissionError):
        return None

    duration_seconds = None
    oshash = None
    phash = None
    if is_video_file(p):
        oshash = compute_oshash(p)
        if _SCAN_PROBE_DURATION:
            duration_seconds = _probe_duration_seconds(p)
        # pHash (opt-in via AMM_SCAN_PHASH) — enables near-duplicate detection of
        # re-encodes. Best-effort: None on any failure, so scanning never breaks.
        if _SCAN_PHASH:
            phash = compute_phash_ffmpeg_sync(p)

    return {
        "path":             str(p),
        "filename":         p.name,
        "size":             file_size,
        "oshash":           oshash,
        "phash":            phash,
        "duration_seconds": duration_seconds,
        "media_type":       det.media_type.value,
        "clean_name":       det.clean_name,
        "normalized_name":  det.normalized_name,   # junk-stripped (D4)
        "tokens":           det.tokens,            # cheap pre-filter set (D4)
        "site":             det.site,
        "performers":       det.performers,
        "scene_title":      det.scene_title,
        "release_date":     det.release_date,
        "year":             det.year,
        "quality":          det.quality,
        "source":           det.source,
        "video_format":     det.video_format,
        "group":            det.group,
        # F8: "folder" when site/title/date were inferred from a directory
        # name (gap-fill only) — the UI shows a 📁 hint on such rows.
        "context_source":   det.context_source,
        # Subtitle files are companions of a video, not standalone scenes (F2).
        # The UI hides a subtitle row when its sibling video is in the same scan
        # (it will be moved along with the video); orphan subtitles still show.
        "is_companion":     is_subtitle_file(p),
        # NFO-derived flags — None when no sidecar found
        "already_organized": nfo_meta is not None,
        "nfo_metadata":      nfo_meta,
    }


def _annotate_catalog_states(files: list[dict]) -> None:
    """Fold the catalog's organised/confirmed knowledge into scanned entries.

    Mutates each entry in place: adds ``user_confirmed`` / ``canonical_scene_id``
    / ``catalog_organized``, and self-heals stale ``organized`` rows (catalog says
    organised but no NFO on disk → clear it). The on-disk NFO is authoritative.
    Best-effort: a catalog hiccup must never break a scan.
    """
    if not files:
        return
    try:
        states = catalog.get_states([f["path"] for f in files])
        for f in files:
            st = states.get(f["path"])
            if not st:
                continue
            f["user_confirmed"] = st["user_confirmed"]
            f["canonical_scene_id"] = st["canonical_scene_id"]
            nfo_present = bool(f.get("already_organized"))
            if st["organized"] and not nfo_present:
                catalog.set_organized(f["path"], False)
                f["catalog_organized"] = False
            else:
                f["catalog_organized"] = st["organized"]
    except Exception as e:
        print(f"WARNING: catalog state annotation failed: {e}")


@app.post("/api/scan")
def scan_directory(req: ScanRequest):
    """
    Scan a directory for adult media files and auto-detect metadata.
    Supports a single directory, a single file, or comma-separated file paths.

    Plain ``def`` (not ``async def``) on purpose: all work here is blocking
    filesystem/CPU work with no awaits, so FastAPI runs it in its anyio worker
    threadpool and a slow scan over a large NAS/FUSE mount never blocks the event
    loop. Identical across Docker/deb/AppImage — pure stdlib, no platform code.

    This non-streaming endpoint is retained for compatibility and the small/
    scripted cases; the UI uses the cancellable streaming pair
    (/api/scan-session + /api/scan-stream) so users can stop a long scan and keep
    the partial results.
    """
    paths, error = _resolve_scan_paths_req(req)
    if error:
        return {"count": 0, "files": [], "error": error}

    _ks = _known_site_norms()   # once per scan, not per file (F8)
    files = [e for e in (_build_file_entry(p, _ks, req.include_hidden) for p in paths)
             if e is not None]

    # ── Catalog (R1): record this scan + apply incremental rescan ───────────
    try:
        catalog.upsert_scanned(files)
    except Exception as e:
        print(f"WARNING: catalog upsert_scanned failed: {e}")
    _annotate_catalog_states(files)
    if req.skip_organized:
        files = [f for f in files if not f.get("already_organized")]

    return {"count": len(files), "files": files}


# ─── Scan — SSE streaming endpoint (cancellable) ───────────────────────
#
# Mirrors the match SSE two-step handshake so the file list/flags are POSTed
# (no URL-size limits) and the GET only carries an opaque token:
#   1. POST /api/scan-session  →  { session_id }
#   2. GET  /api/scan-stream?session_id=…  →  SSE stream of per-file results
#
# The stream lets the UI show results incrementally AND lets the user STOP the
# scan: closing the EventSource disconnects the request, the server detects it
# between files and stops walking, keeping whatever was already scanned. Sessions
# are in-memory and single-use (process-local — same single-worker constraint as
# the match stream, review item P7).
_SCAN_SESSION_TTL = 120  # seconds before an unused scan session expires
_scan_sessions: dict[str, dict] = {}  # { session_id: { "expires": float, "body": ScanRequest } }


@app.post("/api/scan-session")
async def create_scan_session(req: ScanRequest):
    """Stage 1: stash the scan request and return a short-lived session token."""
    session_id = uuid.uuid4().hex
    _scan_sessions[session_id] = {
        "expires": _time.monotonic() + _SCAN_SESSION_TTL,
        "body": req,
    }
    # Opportunistically evict expired sessions (cheap; sessions are rare).
    expired = [k for k, v in _scan_sessions.items() if _time.monotonic() > v["expires"]]
    for k in expired:
        _scan_sessions.pop(k, None)
    return {"session_id": session_id}


@app.get("/api/scan-stream")
async def scan_stream(request: Request, session_id: str = Query(...)):
    """
    Stage 2: stream one result per scanned file so the UI can render
    incrementally and the user can STOP at any time (closing the EventSource).

    Events:
      event: progress  data: {"done": N, "total": M, "filename": "..."}
      event: result    data: {"file": {…file_entry…}}
      event: error     data: {"detail": "..."}                  (path errors)
      event: done      data: {"scanned": N, "total": M, "stopped": bool}

    Per-file work runs in a worker thread (asyncio.to_thread) so the event loop
    stays responsive and the disconnect check between files can fire promptly.
    Identical across Docker/deb/AppImage — pure stdlib + FastAPI, no platform code.
    """
    session = _scan_sessions.pop(session_id, None)
    if session is None or _time.monotonic() > session["expires"]:
        async def _serr():
            yield "event: error\ndata: {\"detail\": \"Session not found or expired\"}\n\n"
        return StreamingResponse(_serr(), media_type="text/event-stream")

    req: ScanRequest = session["body"]

    async def _event_stream():
        # Resolve the candidate file list (blocking rglob/iterdir → thread).
        paths, error = await asyncio.to_thread(_resolve_scan_paths_req, req)
        if error:
            yield f"event: error\ndata: {json.dumps({'detail': error})}\n\n"
            return

        total = len(paths)
        scanned = 0
        stopped = False
        # Why files were skipped, for the "K skipped" summary (F9). Directories
        # (rglob includes them) are structural, not skipped *files*, so they are
        # deliberately not tallied here.
        skip_non_media = 0
        skip_hidden = 0
        skip_unreadable = 0
        batch: list[dict] = []   # accumulate for a single catalog upsert
        _scan_known_sites = _known_site_norms()   # once per scan, not per file (F8)

        async def _flush_upsert():
            if batch:
                # Persist scan-derived columns for everything processed so far,
                # even on stop, so the catalog stays consistent. Best-effort.
                try:
                    await asyncio.to_thread(catalog.upsert_scanned, list(batch))
                except Exception as e:
                    print(f"WARNING: catalog upsert (stream) failed: {e}")
                batch.clear()

        try:
            for i, p in enumerate(paths):
                # Stop point: the client closed the stream → keep partial results.
                if await request.is_disconnected():
                    stopped = True
                    break

                entry = await asyncio.to_thread(_build_file_entry, p, _scan_known_sites,
                                                req.include_hidden)
                if entry is None:
                    # Classify WHY this candidate was skipped, for the summary
                    # (F9). Order mirrors _build_file_entry's own skip checks
                    # (not-a-file → hidden → non-media → otherwise unreadable), so
                    # the reason matches what actually caused the None.
                    try:
                        if not p.is_file():
                            pass  # directory / non-regular — not a skipped file
                        elif p.name.startswith('.') and not req.include_hidden:
                            skip_hidden += 1
                        elif p.suffix.lower() not in _SCAN_MEDIA_EXTS:
                            skip_non_media += 1
                        else:
                            skip_unreadable += 1   # media ext but stat/detect failed
                    except OSError:
                        skip_unreadable += 1
                    # Advance progress only.
                    yield (
                        "event: progress\ndata: "
                        + json.dumps({"done": i + 1, "total": total, "filename": p.name})
                        + "\n\n"
                    )
                    continue

                # Annotate from catalog (organised/confirmed) for this one entry,
                # then queue it for the batched on-disk upsert.
                await asyncio.to_thread(_annotate_catalog_states, [entry])
                batch.append(entry)
                if len(batch) >= 50:
                    await _flush_upsert()

                # Incremental rescan: hide already-organised files from the list
                # (still counted + upserted), matching /api/scan?skip_organized.
                if req.skip_organized and entry.get("already_organized"):
                    yield (
                        "event: progress\ndata: "
                        + json.dumps({"done": i + 1, "total": total, "filename": entry["filename"]})
                        + "\n\n"
                    )
                    continue

                scanned += 1
                yield (
                    "event: progress\ndata: "
                    + json.dumps({"done": i + 1, "total": total, "filename": entry["filename"]})
                    + "\n\n"
                )
                yield "event: result\ndata: " + json.dumps({"file": entry}) + "\n\n"
        finally:
            await _flush_upsert()

        yield (
            "event: done\ndata: "
            + json.dumps({
                "scanned": scanned, "total": total, "stopped": stopped,
                # Breakdown of skipped candidates (F9). Sums to (files − scanned)
                # for a flat scan; directories and skip_organized files are not
                # counted here (they aren't "skipped media").
                "skipped": {
                    "non_media": skip_non_media,
                    "hidden": skip_hidden,
                    "unreadable": skip_unreadable,
                },
            })
            + "\n\n"
        )

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─── Catalog Endpoints (R1) ────────────────────────────────────────────

# ── Store stats (F16): entry counts + on-disk bytes per persistent store. ──
# The thumbnails rglob can be slow on big DATA_DIRs, so the whole block is
# cached for 60 s; maintenance endpoints invalidate it.
_store_stats_cache: dict = {"at": 0.0, "data": None}


def _fsize(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _dir_stats(d: Path) -> tuple[int, int]:
    """(file_count, total_bytes) for a directory tree; (0, 0) when absent."""
    files = 0
    total = 0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                files += 1
                total += _fsize(f)
    except OSError:
        pass
    return files, total


def _store_stats() -> dict:
    now = _time.time()
    if _store_stats_cache["data"] is not None and now - _store_stats_cache["at"] < 60:
        return _store_stats_cache["data"]
    mc = _match_cache_store.get()
    th_count, th_bytes = _dir_stats(DATA_DIR / "thumbnails")
    data = {
        "match_cache": {
            "count": len(mc),
            "confirmed": sum(1 for v in mc.values() if v.get("user_confirmed")),
            "bytes": _fsize(_MATCH_CACHE_FILE),
        },
        "aliases": {
            # Learned knowledge, one tile: performer + site aliases + portraits.
            "count": (len(_performer_aliases_store.get())
                      + len(_site_aliases_store.get())
                      + len(_performer_images_store.get())),
            "bytes": (_fsize(_PERFORMER_ALIASES_FILE) + _fsize(_SITE_ALIASES_FILE)
                      + _fsize(_PERFORMER_IMAGES_FILE)),
        },
        "known_sites": {"count": len(_load_known_sites()),
                        "bytes": _fsize(KNOWN_SITES_FILE)},
        "history": {"count": len(history.entries),
                    "bytes": _fsize(DATA_DIR / "history.json")},
        "thumbnails": {"count": th_count, "bytes": th_bytes},
    }
    _store_stats_cache.update(at=now, data=data)
    return data


@app.get("/api/catalog/stats")
async def catalog_stats():
    """Aggregate catalog counts for the UI (total / organised / confirmed /
    duplicate groups) + per-store counts/sizes (F16). Read-only; safe defaults
    if the catalog is disabled."""
    out = catalog.stats()
    try:
        out["stores"] = _store_stats()
    except Exception as e:   # stats must never break the Library modal
        print(f"WARNING: store stats failed: {e}")
    return out


@app.post("/api/maintenance/clear-match-cache")
async def clear_match_cache():
    """Drop every NON-confirmed match-cache entry (F16). Zero parameters —
    nothing user-controllable; user_confirmed entries always survive."""
    removed = {"n": 0}

    def _apply(cache: dict) -> bool:
        drop = [k for k, v in cache.items() if not v.get("user_confirmed")]
        for k in drop:
            del cache[k]
        removed["n"] = len(drop)
        return bool(drop)

    _match_cache_store.mutate(_apply)
    _store_stats_cache["data"] = None
    return {"removed": removed["n"]}


@app.post("/api/maintenance/clear-thumbnails")
async def clear_thumbnails():
    """Delete all extracted preview thumbnails (F16). Zero parameters; the
    directory itself is kept. Confirmed matches lose their stored preview
    image (the UI's confirm text says so) — matching is unaffected."""
    tdir = DATA_DIR / "thumbnails"
    _, freed = _dir_stats(tdir)
    removed = 0
    if tdir.is_dir():
        try:
            for child in tdir.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                    removed += 1
                except OSError as e:
                    print(f"WARNING: clear-thumbnails skipped {child.name}: {e}")
        except OSError as e:
            print(f"WARNING: clear-thumbnails failed: {e}")
    _store_stats_cache["data"] = None
    return {"freed_bytes": freed, "removed": removed}


@app.get("/api/catalog/duplicates")
async def catalog_duplicates():
    """Groups of files sharing a content fingerprint (same oshash, ≥2 copies)."""
    return {"groups": catalog.find_duplicates()}


class ResolveDuplicatesRequest(BaseModel):
    """Request body for /api/catalog/resolve-duplicates (F16)."""
    keep: str
    remove: list[str]
    mode: str  # "delete" | "hardlink"

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("delete", "hardlink"):
            raise ValueError("mode must be 'delete' or 'hardlink'")
        return v

    @field_validator("remove")
    @classmethod
    def validate_remove(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("remove must not be empty")
        if len(v) > 100:
            raise ValueError("too many files in one request")
        if len(set(v)) != len(v):
            raise ValueError("remove contains duplicates")
        return v


@app.post("/api/catalog/resolve-duplicates")
async def resolve_duplicates(req: ResolveDuplicatesRequest):
    """Resolve a duplicate group: keep one copy, delete or hardlink the rest (F16).

    AMM's first file-DELETING endpoint, so verification is a separate phase
    before any mutation, and the client's grouping is never trusted:
      • every path must pass the allowed-roots check;
      • `keep` is compared to each `remove` by resolved path (a string check
        wouldn't stop symlink/`..` aliases from deleting the kept copy);
      • every `remove`'s oshash is recomputed NOW and must equal `keep`'s —
        any mismatch 409s with per-file detail and nothing is touched;
      • hardlink mode additionally requires same-filesystem (st_dev), and the
        swap is atomic (link to tmp → os.replace) so no path ever disappears.
    Each affected file gets a `dedupe_<mode>` history entry (non-revertible by
    construction — the action is outside REVERTIBLE_ACTIONS; delete is honest
    about being final). Deletes are dropped from the catalog.
    """
    keep_path = Path(req.keep)
    remove_paths = [Path(p) for p in req.remove]

    # ── Phase 1: validate + re-verify (no filesystem mutation here) ──────────
    for p in [keep_path, *remove_paths]:
        if not _is_allowed_path(p):
            raise HTTPException(status_code=403,
                                detail=f"Path not in an allowed media directory: {p}")
    if not keep_path.is_file():
        raise HTTPException(status_code=404, detail=f"Keep file not found: {keep_path}")
    keep_resolved = keep_path.resolve()
    for p in remove_paths:
        if p.resolve() == keep_resolved:
            raise HTTPException(status_code=400,
                                detail="A file to remove is the same file as the one to keep")

    keep_oshash = await asyncio.to_thread(compute_oshash, keep_path)
    if not keep_oshash:
        raise HTTPException(status_code=409,
                            detail={"code": "hash_failed", "path": str(keep_path)})
    keep_stat = keep_path.stat()

    problems: list[dict] = []
    for p in remove_paths:
        if not p.is_file():
            problems.append({"path": str(p), "code": "not_found"})
            continue
        oshash = await asyncio.to_thread(compute_oshash, p)
        if oshash != keep_oshash:
            problems.append({"path": str(p), "code": "hash_mismatch"})
            continue
        if req.mode == "hardlink" and p.stat().st_dev != keep_stat.st_dev:
            problems.append({"path": str(p), "code": "not_same_fs"})
    if problems:
        raise HTTPException(status_code=409, detail={
            "code": problems[0]["code"], "files": problems,
        })

    # ── Phase 2: execute (per-file best effort, everything logged) ──────────
    results: list[dict] = []
    history_batch: list[tuple] = []
    freed = 0
    for p in remove_paths:
        try:
            size = p.stat().st_size
            if req.mode == "delete":
                p.unlink()
                try:
                    catalog.forget(str(p))
                except Exception as e:
                    print(f"WARNING: catalog forget after dedupe failed: {e}")
                freed += size
            else:
                st = p.stat()
                if st.st_dev == keep_stat.st_dev and st.st_ino == keep_stat.st_ino:
                    results.append({"path": str(p), "ok": True, "code": "already_linked"})
                    continue
                tmp = p.with_suffix(p.suffix + ".amm_ln")
                os.link(keep_path, tmp)
                os.replace(tmp, p)
                freed += size
            results.append({"path": str(p), "ok": True, "code": "ok"})
            history_batch.append((p, keep_path, f"dedupe_{req.mode}", True))
        except Exception as e:
            results.append({"path": str(p), "ok": False, "code": "error"})
            history_batch.append((p, keep_path, f"dedupe_{req.mode}", False, str(e)))
            print(f"WARNING: dedupe {req.mode} failed for {p}: {e}")

    if history_batch:
        history.add_entries(history_batch)

    return {
        "success": all(r["ok"] for r in results),
        "results": results,
        "freed_bytes": freed,
    }


# ─── Match Endpoint ────────────────────────────────────────────────────

def _stashdb_scene_to_dict(s) -> dict:
    """Convert a StashDBScene to the common scene dict used throughout the app."""
    return _apply_performer_order({
        "id": s.id,
        "title": s.title,
        "site": s.site,
        "network": s.network,
        "performers": s.performers,
        "performer_genders": s.performer_genders,
        "release_date": s.release_date,
        "tags": s.tags,
        "duration": s.duration,  # seconds — used for duration match scoring (D1)
        "poster_url": s.poster_url,
        "thumbnail_url": s.poster_url,  # StashDB doesn't separate thumb/poster
        "description": s.description,   # synopsis → NFO <plot> (F4)
        "source": "stashdb",            # → NFO <uniqueid type> (F4)
        # F6: scene code / director / studio URL / per-scene performer credits.
        # TPDB scenes simply lack these keys — every consumer .get()s them.
        "code": s.code,
        "director": s.director,
        "url": s.url,
        "performer_credits": s.performer_credits,
        # F7: fanart backdrop (second-widest image) → NFO <fanart>.
        "fanart_url": s.fanart_url,
    })


# Canonical UUID matcher — StashDB scene IDs are UUIDs. Used to extract the id
# from a pasted scene URL (https://stashdb.org/scenes/{UUID}) or a bare UUID, and
# to reject anything else before it ever reaches the GraphQL query (the GraphQL
# ID type is just a string, so validating here keeps malformed/abusive input out).
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _extract_stashdb_scene_id(raw: str) -> Optional[str]:
    """Pull a StashDB scene UUID out of a pasted URL or a bare id.

    Accepts:
      • https://stashdb.org/scenes/<uuid>  (with/without trailing slash, query,
        fragment, or extra path segments)
      • a bare <uuid>
    Returns the lowercased UUID, or None if no valid UUID is present. Host is not
    enforced (mirrors/self-hosted Stash-box instances exist), but the value must
    be a real UUID — so this can't be used to probe arbitrary URLs/paths.
    """
    if not raw:
        return None
    m = _UUID_RE.search(raw.strip())
    return m.group(0).lower() if m else None


class StashDBLookupRequest(BaseModel):
    """Request body for /api/stashdb/scene — a pasted scene URL or bare UUID."""
    url: str

    @field_validator("url")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("A StashDB scene URL or ID is required")
        return v.strip()


@app.post("/api/stashdb/scene")
async def stashdb_scene_lookup(req: StashDBLookupRequest):
    """
    Resolve a StashDB scene URL (https://stashdb.org/scenes/{UUID}) or a bare
    UUID to full scene metadata, for the manual-edit "Fetch from StashDB" action.

    Returns the same scene dict shape used everywhere else so the client can
    populate the manual-edit fields directly. Requires a configured StashDB key
    (env var or Settings UI) — the key never leaves the server.
    """
    if not stashdb:
        raise HTTPException(
            status_code=503,
            detail="StashDB API key not configured. Set it via STASHDB_API_KEY or the Settings UI.",
        )

    scene_id = _extract_stashdb_scene_id(req.url)
    if not scene_id:
        raise HTTPException(
            status_code=400,
            detail="Could not find a scene ID. Paste a link like https://stashdb.org/scenes/<id>.",
        )

    try:
        scene = await stashdb.find_scene_by_id(scene_id)
    except Exception as exc:
        # Typed failure (roadmap-2 F15): classify auth/rate_limit/network so the
        # Manual Edit status line shows the same honest message the match rows
        # do. Raw exception text stays server-side — it can echo GraphQL
        # internals and belongs in the log, not the client.
        kind = _classify_stashdb_error(exc)
        print(f"StashDB scene lookup error ({kind}): {exc}")
        raise HTTPException(status_code=502, detail={"code": kind})

    if scene is None:
        raise HTTPException(status_code=404, detail="No StashDB scene found for that ID.")

    return {"scene": _stashdb_scene_to_dict(scene)}


# ── ThePornDB scene-by-URL lookup (manual-edit "Fetch") ────────────────────────
# ThePornDB is a REST API (api.theporndb.net, Bearer auth) and its scene URLs use
# a *slug* — https://theporndb.net/scenes/<slug> — not a UUID. We take the last
# path segment and look the scene up; if the API can't resolve the slug directly,
# we fall back to a text search on the de-slugified words so a pasted link still
# works either way. Same UX as the StashDB fetch.
_TPDB_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")


def _tpdb_scene_to_dict(s) -> dict:
    """Convert a TPDBScene to the common scene dict used throughout the app."""
    return _apply_performer_order({
        "id": s.id,
        "title": s.title,
        "site": s.site,
        "network": s.network,
        "performers": s.performers,
        "performer_genders": s.performer_genders,
        "release_date": s.release_date,
        "tags": s.tags,
        "duration": s.duration,
        "poster_url": s.poster_url_large,
        "thumbnail_url": s.thumbnail_url_small,
        "description": s.description,   # synopsis → NFO <plot> (F4)
        "source": "tpdb",               # → NFO <uniqueid type> (F4)
        # F7: scene page URL + fanart (background when distinct from poster).
        "url": s.url,
        "fanart_url": s.fanart_url_large,
    })


def _extract_tpdb_scene_slug(raw: str) -> Optional[str]:
    """Pull the scene slug (or id) out of a pasted ThePornDB URL or bare token.

    For a ``…/scenes/<slug>`` URL, takes the segment immediately after ``scenes``;
    otherwise (a bare slug/id) takes the last path segment. The result is validated
    against a strict slug/id charset (letters, digits, ``-``, ``_``) and rejected
    if it is the literal ``scenes`` (i.e. a URL with no id). Because only the part
    up to the next ``/`` is kept and dots/slashes/percent are disallowed, the value
    can never carry a path separator or traversal sequence into the REST path
    ``/scenes/<slug>``. Host is not enforced (the slug carries the identity).
    """
    if not raw:
        return None
    s = raw.strip().split("?", 1)[0].split("#", 1)[0]
    marker = "/scenes/"
    low = s.lower()
    if marker in low:
        seg = s[low.index(marker) + len(marker):].split("/", 1)[0].strip()
    else:
        seg = (s.rstrip("/").rsplit("/", 1)[-1] if "/" in s else s).strip()
    if not seg or seg.lower() == "scenes":
        return None
    return seg if _TPDB_SLUG_RE.fullmatch(seg) else None


@app.post("/api/tpdb/scene")
async def tpdb_scene_lookup(req: StashDBLookupRequest):
    """
    Resolve a ThePornDB scene URL (https://theporndb.net/scenes/{slug}) — or a
    bare slug/id — to full scene metadata, for the manual-edit "Fetch from TPDB"
    action. Returns the shared scene dict shape so the client populates the
    manual-edit fields directly. Requires a configured TPDB key; the key never
    leaves the server.
    """
    if not tpdb:
        raise HTTPException(
            status_code=503,
            detail="ThePornDB API key not configured. Set it via TPDB_API_KEY or the Settings UI.",
        )

    slug = _extract_tpdb_scene_slug(req.url)
    if not slug:
        raise HTTPException(
            status_code=400,
            detail="Could not find a scene from that link. Paste a link like "
                   "https://theporndb.net/scenes/<slug>.",
        )

    # 1) Direct lookup by slug/id. (get_scene already swallows transport errors
    #    and returns None, so a failure here just falls through to the search.)
    scene = await tpdb.get_scene(slug)

    # 2) Fallback: de-slugify ("a-b-c" → "a b c") and text-search, so the link
    #    still resolves even when the API only accepts an internal id.
    if scene is None:
        query = slug.replace("-", " ").replace("_", " ").strip()
        results = await tpdb.search_scene(query=query)
        scene = results[0] if results else None

    if scene is None:
        # Typed failure (roadmap-2 F15): if the provider CALL failed, say so —
        # previously an expired key here surfaced as a plain 404 ("no scene
        # found"), a lie. A genuine miss (both lookups ran clean) stays a 404.
        if tpdb.last_error:
            raise HTTPException(status_code=502, detail={"code": tpdb.last_error})
        raise HTTPException(status_code=404, detail="No ThePornDB scene found for that link.")

    return {"scene": _tpdb_scene_to_dict(scene)}


def _dedup_alternatives(results: list[dict], best: dict) -> list[dict]:
    """Build the alternatives list keyed by scene ``id`` (review item D8).

    The old filter compared whole dicts (``r != best_match``), which is brittle:
    equality is order-sensitive on list fields like ``performers``/``tags``, so a
    re-ordered duplicate slipped through while a structurally-identical alt could
    be dropped. We instead exclude the chosen match by identity *and* by id, and
    drop any further duplicates sharing an id. Items without a usable id are kept
    as-is (can't be deduped reliably) — only the chosen object is removed by
    identity in that case.
    """
    best_id = best.get("id") if isinstance(best, dict) else None
    seen: set = set()
    out: list[dict] = []
    for r in results:
        if r is best:
            continue
        rid = r.get("id")
        if rid is not None:
            if rid == best_id or rid in seen:
                continue
            seen.add(rid)
        out.append(r)
    return out


def _cheap_phash(file_data: dict, file_path: Path) -> Optional[str]:
    """F3: resolve a pHash without running ffmpeg — request payload, then
    catalog row. A hash is reused only when the byte size it was computed at
    still equals the file on disk (bytes changed → hash invalid), and only in
    wire format (16 lowercase hex) — the payload crosses the API boundary, so
    it must never reach StashDB unvalidated."""
    try:
        st_size = file_path.stat().st_size
    except OSError:
        return None
    claimed = file_data.get("phash")
    if (isinstance(claimed, str)
            and re.fullmatch(r"[0-9a-f]{16}", claimed)
            and file_data.get("size") == st_size):
        return claimed
    phash = catalog.get_phash(str(file_path), size=st_size)
    if phash is not None and not re.fullmatch(r"[0-9a-f]{16}", phash):
        return None
    return phash


# One StashDB fingerprint request can carry many files (F4).
_FP_BATCH_SIZE = 40


async def _prefetch_stashdb_fingerprints(files: list[dict], refresh: bool) -> dict[int, list]:
    """F4: resolve the fingerprint tier for a whole match run in ⌈pending/40⌉
    round-trips instead of one per file.

    Only cheaply-known fingerprints enter the batch (scan payload / catalog —
    never live pHash computation); cache-hit files are skipped entirely (they
    never reach the network). Returns {file_index: [StashDBScene, ...]} — an
    EMPTY list is an authoritative miss (per-file skips straight to text
    search). On a batch transport error the affected files are simply absent,
    so they keep today's per-file path and accuracy never degrades.
    """
    if not stashdb:
        return {}
    pending: list[tuple[int, list[dict]]] = []
    for i, f in enumerate(files):
        try:
            oshash = f.get("oshash")
            if not refresh and oshash and _match_cache_lookup(oshash, f):
                continue
            p = Path(f.get("path", ""))
            if not p.is_file():
                continue   # parity: the per-file path refuses fingerprints too
            group: list[dict] = []
            if isinstance(oshash, str) and re.fullmatch(r"[0-9a-f]{16}", oshash):
                group.append({"algorithm": "OSHASH", "hash": oshash})
            else:
                oh = await asyncio.to_thread(compute_oshash, p)
                if oh:
                    group.append({"algorithm": "OSHASH", "hash": oh})
            ph = _cheap_phash(f, p)
            if ph:
                group.append({"algorithm": "PHASH", "hash": ph})
            if group:
                pending.append((i, group))
        except Exception as exc:
            print(f"WARNING: fingerprint prefetch skipped {f.get('filename', '?')}: {exc!r}")
    out: dict[int, list] = {}
    for start in range(0, len(pending), _FP_BATCH_SIZE):
        chunk = pending[start:start + _FP_BATCH_SIZE]
        res = await stashdb.find_by_fingerprints_batch([g for _, g in chunk])
        if res is None:
            continue   # transport error → these files use the per-file path
        for (idx, _), scenes in zip(chunk, res):
            out[idx] = scenes
    return out


async def _match_one_stashdb(file_data: dict, sem: asyncio.Semaphore,
                             prefetched_fp: Optional[list] = None) -> dict:
    """Match a single file against StashDB using fingerprint then text search.

    ``prefetched_fp`` (F4): when not None it replaces the per-file fingerprint
    lookup — non-empty is a batch hit, empty is an authoritative batch miss
    (skip straight to text search). None means "no batch ran for this file"
    and keeps the full per-file fingerprint path.
    """
    async with sem:
        no_match = {"original": file_data, "match": None, "confidence": 0, "alternatives": []}

        # ── 1. Fingerprint search (highest accuracy) ──────────────────
        file_path = Path(file_data.get("path", ""))
        fp_results = prefetched_fp
        if fp_results is None and file_path.is_file():
            # OSHash reads the file head+tail (blocking disk I/O); run it in a
            # worker thread so a slow NAS read doesn't stall the event loop.
            oshash = await asyncio.to_thread(compute_oshash, file_path)
            # pHash reuse (F3): scan payload → catalog → live compute.
            phash = _cheap_phash(file_data, file_path)
            if phash is None:
                # pHash computation is CPU-heavy; cap the live fallback.
                try:
                    # Budget depends on the algorithm (F14): the stash-compatible
                    # sprite needs 25 sequential ffmpeg seeks (slow on NAS mounts).
                    phash = await asyncio.wait_for(
                        compute_phash_ffmpeg(file_path), timeout=PHASH_MATCH_TIMEOUT)
                except asyncio.TimeoutError:
                    phash = None

            if oshash or phash:
                fp_results = await stashdb.find_by_fingerprint(oshash=oshash, phash=phash)

        if fp_results:
            best = fp_results[0]
            scene_dict = _stashdb_scene_to_dict(best)
            ms = score_match(file_data, scene_dict, alias_resolver=_alias_lookup,
                                      site_resolver=_site_lookup)
            alts = _dedup_alternatives(
                [_stashdb_scene_to_dict(s) for s in fp_results[1:5]],
                scene_dict,
            )
            # Register the matched studio so the site autocomplete is
            # populated for StashDB users too (parity with TPDB, F7).
            _add_known_site(scene_dict.get("site") or "", scene_dict.get("network") or "")
            return {
                "original": file_data,
                "match": scene_dict,
                "confidence": round(max(ms.agreement * 100, 90.0), 1),
                "coverage": round(ms.coverage, 3),
                "match_fields": ms.fields,
                "alternatives": alts,
                # Exact oshash/phash hit — near-certain regardless of the
                # cascade %. The UI surfaces this as a "verified" badge.
                "match_method": "fingerprint",
            }

        # ── 2. Resolve search title/site ──────────────────────────────
        # The detector now extracts site/title/date for "Studio - Title (Date)"
        # filenames too (D4 consolidation), so we just read its output and fall
        # back to the junk-stripped normalized name when there's no scene title.
        parsed_site  = file_data.get("site")
        parsed_title = (
            file_data.get("scene_title")
            or file_data.get("normalized_name")
            or file_data.get("clean_name", "")
        )

        # Build an enriched copy of file_data for cascade scoring
        scoring_data = dict(file_data)
        if parsed_title:
            scoring_data["scene_title"] = parsed_title

        # ── 3. Primary search: title + studio ────────────────────────
        performer = (file_data.get("performers") or [""])[0]
        results = await stashdb.search_scene(
            query=parsed_title,
            performer=performer,
            studio=parsed_site,
        )

        # ── 4. Secondary search: title only (if primary returned nothing) ──
        if not results and parsed_site:
            results = await stashdb.search_scene(query=parsed_title)

        if not results:
            # F15: distinguish "provider call failed" (auth/rate_limit/network)
            # from "scene not in database" — the UI renders these differently.
            if stashdb.last_error:
                no_match["lookup_error"] = stashdb.last_error
            return no_match

        results_dicts = [_stashdb_scene_to_dict(s) for s in results[:5]]

        # ── 5. Score with enriched metadata ─────────────────────────
        # find_best_match() now includes the title-only fallback internally
        # (review item D2), so sparse/title-only files are handled here for both
        # StashDB and TPDB — no endpoint-specific fallback copy needed.
        best_result = find_best_match(scoring_data, results_dicts,
                                      alias_resolver=_alias_lookup,
                                      site_resolver=_site_lookup)
        if best_result:
            best_match, ms = best_result
            # Register the matched studio for the site autocomplete (parity with
            # TPDB, F7) — same helper the TPDB path uses; skips empty names.
            _add_known_site(best_match.get("site") or "", best_match.get("network") or "")
            return {
                "original": file_data,
                "match": best_match,
                "confidence": round(ms.agreement * 100, 1),
                "coverage": round(ms.coverage, 3),
                "match_fields": ms.fields,
                "alternatives": _dedup_alternatives(results_dicts, best_match),
            }

        return no_match


async def _match_one_tpdb(file_data: dict, sem: asyncio.Semaphore, auto_match: bool) -> dict:
    """Match a single file against TPDB (auto filename-parse, then text search).

    Extracted so the plain and SSE endpoints share ONE implementation (was
    duplicated) and both can be wrapped by the match cache uniformly.
    """
    async with sem:
        filename = file_data.get("filename", "")

        # Try automatic matching via filename parsing
        if auto_match:
            auto = await tpdb.parse_filename(filename)
            if auto:
                net = auto.network or ""
                _add_known_site(auto.site, net)
                # One scene-dict builder for every TPDB path (F4) — so description
                # + source ride along automatically and can't drift from the shape
                # the search path / lookup endpoint produce.
                scene_dict = _tpdb_scene_to_dict(auto)
                ms = score_match(file_data, scene_dict, alias_resolver=_alias_lookup,
                                      site_resolver=_site_lookup)
                return {
                    "original": file_data,
                    "match": scene_dict,
                    "confidence": round(ms.agreement * 100, 1),
                    "coverage": round(ms.coverage, 3),
                    "match_fields": ms.fields,
                    "alternatives": [],
                }

        # Fallback to search. Prefer the parsed scene title; otherwise use the
        # junk-stripped normalized name (D4) before the raw clean name.
        search_query = (
            file_data.get("scene_title")
            or file_data.get("normalized_name")
            or file_data.get("clean_name", "")
        )
        site_filter  = file_data.get("site")
        search_results = await tpdb.search_scene(query=search_query, site=site_filter)

        if search_results:
            results_dicts = [_tpdb_scene_to_dict(s) for s in search_results[:5]]
            best_result = find_best_match(file_data, results_dicts,
                                          alias_resolver=_alias_lookup,
                                      site_resolver=_site_lookup)
            if best_result:
                best_match, ms = best_result
                return {
                    "original": file_data,
                    "match": best_match,
                    "confidence": round(ms.agreement * 100, 1),
                    "coverage": round(ms.coverage, 3),
                    "match_fields": ms.fields,
                    "alternatives": _dedup_alternatives(results_dicts, best_match),
                }
            return {"original": file_data, "match": None, "confidence": 0, "alternatives": results_dicts}

        # F15: no results at all — if the provider call itself failed, say so
        # instead of letting it masquerade as "scene not in database".
        no_match = {"original": file_data, "match": None, "confidence": 0, "alternatives": []}
        if tpdb.last_error:
            no_match["lookup_error"] = tpdb.last_error
        return no_match


async def _cached_match(file_data: dict, do_match, refresh: bool, source: str,
                        updates: dict) -> dict:
    """Wrap a per-file match with the persistent cache (D3).

    On a cache hit (unless ``refresh``) the stored scene is returned without any
    pHash/API work. On a miss ``do_match()`` runs and a successful result is
    queued in ``updates`` for a single batched write by the caller.
    """
    oshash = file_data.get("oshash")
    if not refresh and oshash:
        cached = _match_cache_lookup(oshash, file_data)
        if cached is not None:
            return cached
    result = await do_match()
    if oshash and isinstance(result, dict) and result.get("match"):
        updates[oshash] = _make_cache_entry(
            result["match"], source, result.get("confidence", 0),
            result.get("match_method"),
            coverage=result.get("coverage"),
            match_fields=result.get("match_fields"),
        )
    return result


@app.post("/api/match")
async def match_scenes(req: MatchRequest):
    """
    Match detected files against TPDB or StashDB.
    All files are matched concurrently (up to 5 at a time) to avoid rate limits.
    """
    use_stashdb = req.datasource == "stashdb"
    cache_updates: dict = {}   # batched cache writes (one flush after gather)

    if use_stashdb:
        if not stashdb:
            raise HTTPException(
                status_code=503,
                detail="StashDB API key not configured. Set STASHDB_API_KEY environment variable."
            )
        sem = asyncio.Semaphore(5)
        # F4: one batched fingerprint query for the whole run; per-file tasks
        # receive their slice (or None on batch failure → per-file fallback).
        prefetched = await _prefetch_stashdb_fingerprints(req.files, req.refresh)
        matched_files = await asyncio.gather(*[
            _cached_match(f, lambda f=f, i=i: _match_one_stashdb(f, sem, prefetched.get(i)),
                          req.refresh, "stashdb", cache_updates)
            for i, f in enumerate(req.files)
        ])
        _match_cache_flush(cache_updates)
        return {"matches": list(matched_files)}

    # ── TPDB path ─────────────────────────────────────────────────────
    if not tpdb:
        raise HTTPException(
            status_code=503,
            detail="TPDB API key not configured. Set TPDB_API_KEY environment variable."
        )

    # Semaphore: max 5 concurrent TPDB requests
    sem = asyncio.Semaphore(5)

    # Run all matches concurrently (cache-wrapped), preserving original order
    matched_files = await asyncio.gather(*[
        _cached_match(f, lambda f=f: _match_one_tpdb(f, sem, req.auto_match),
                      req.refresh, "tpdb", cache_updates)
        for f in req.files
    ])
    _match_cache_flush(cache_updates)
    return {"matches": list(matched_files)}


# ─── Match — SSE streaming endpoint ─────────────────────────────────
#
# Two-step handshake avoids URL query-string size limits (nginx default 8 KB):
#   1. POST /api/match-session  →  { session_id: "<uuid>" }
#   2. GET  /api/match-stream?session_id=<uuid>  →  SSE stream
#
# Sessions are in-memory (no disk I/O), consumed on first stream open,
# and auto-expire after 60 s to prevent orphan accumulation.

_SSE_MAX_FILES     = 500   # hard cap per session
_SSE_SESSION_TTL   = 60    # seconds before an unused session expires
_match_sessions: dict[str, dict] = {}  # { session_id: { "expires": float, "body": MatchRequest } }


@app.post("/api/match-session")
async def create_match_session(req: MatchRequest):
    """
    Stage 1: store the file list server-side and return a short-lived session
    token that the EventSource GET can reference without URL-size limits.
    """
    session_id = uuid.uuid4().hex
    _match_sessions[session_id] = {
        "expires": _time.monotonic() + _SSE_SESSION_TTL,
        "body": req,
    }
    # Evict expired sessions opportunistically (O(n) but sessions are rare)
    expired = [k for k, v in _match_sessions.items() if _time.monotonic() > v["expires"]]
    for k in expired:
        _match_sessions.pop(k, None)
    # Honest oversubmission (F11): the stream truncates at _SSE_MAX_FILES as a
    # DoS guard — echo both counts so a client that sent more can SEE it (the
    # app's own client chunks at the same size and never trips this).
    return {
        "session_id": session_id,
        "accepted": min(len(req.files), _SSE_MAX_FILES),
        "submitted": len(req.files),
    }


@app.get("/api/match-stream")
async def match_stream(request: Request, session_id: str = Query(...)):
    """
    Server-Sent Events endpoint that streams one JSON result event per file
    as it is matched against TPDB or StashDB.  Emits three event types:

      event: progress
      data: {"done": N, "total": M, "filename": "..."}

      event: result
      data: {"index": N, "match": {...}}   (same shape as /api/match items)

      event: done
      data: {"matched": N, "total": M}

    The file list is registered via POST /api/match-session; only the opaque
    session_id token is passed in the query string to avoid URL-size limits.
    """
    # Consume the session (single-use; deleted immediately to free memory)
    session = _match_sessions.pop(session_id, None)
    if session is None or _time.monotonic() > session["expires"]:
        async def _serr():
            yield "event: error\ndata: {\"detail\": \"Session not found or expired\"}\n\n"
        return StreamingResponse(_serr(), media_type="text/event-stream")

    req: MatchRequest = session["body"]
    use_stashdb = req.datasource == "stashdb"

    if use_stashdb and not stashdb:
        async def _err():
            yield "event: error\ndata: {\"detail\": \"StashDB API key not configured. Set STASHDB_API_KEY.\"}\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")

    if not use_stashdb and not tpdb:
        async def _err():  # type: ignore[no-redef]
            yield "event: error\ndata: {\"detail\": \"TPDB API key not configured\"}\n\n"
        return StreamingResponse(_err(), media_type="text/event-stream")

    files      = req.files[:_SSE_MAX_FILES]
    auto_match = req.auto_match
    total      = len(files)
    sem: asyncio.Semaphore = asyncio.Semaphore(5)
    q: asyncio.Queue = asyncio.Queue()
    cache_updates: dict = {}   # batched cache writes, flushed when the stream ends

    async def _match_one_sse(idx: int, file_data: dict,
                             prefetched_fp: Optional[list] = None) -> None:
        result = {"original": file_data, "match": None, "confidence": 0, "alternatives": []}
        try:
            if use_stashdb:
                result = await _cached_match(
                    file_data, lambda: _match_one_stashdb(file_data, sem, prefetched_fp),
                    req.refresh, "stashdb", cache_updates)
            else:
                result = await _cached_match(
                    file_data, lambda: _match_one_tpdb(file_data, sem, auto_match),
                    req.refresh, "tpdb", cache_updates)
        except Exception as exc:
            # F15: never silently downgrade a crash to "No match found" — tag
            # the row so the UI says the lookup errored, and log server-side.
            result["lookup_error"] = "internal"
            print(f"WARNING: match failed for {file_data.get('filename', '?')}: {exc!r}")

        await q.put((idx, result))

    async def _event_stream():
        # F4: one batched fingerprint query up-front (⌈pending/40⌉ round-trips)
        # so the per-file tasks below skip their individual fingerprint calls.
        # Defensive: a prefetch crash must never kill the stream — files just
        # fall back to the per-file path.
        prefetched: dict[int, list] = {}
        if use_stashdb:
            try:
                prefetched = await _prefetch_stashdb_fingerprints(files, req.refresh)
            except Exception as exc:
                print(f"WARNING: fingerprint prefetch failed: {exc!r}")

        # Kick off all match tasks concurrently (semaphore limits parallelism)
        tasks = [asyncio.create_task(_match_one_sse(i, f, prefetched.get(i)))
                 for i, f in enumerate(files)]

        # Ordered slots so we can reconstruct the final list client-side
        ordered: list[dict | None] = [None] * total
        done_count = 0

        try:
            while done_count < total:
                # Check if the client disconnected
                if await request.is_disconnected():
                    for t in tasks:
                        t.cancel()
                    return

                try:
                    idx, result = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Safety valve: client will reconnect via EventSource
                    yield "event: error\ndata: {\"detail\": \"timeout\"}\n\n"
                    for t in tasks:
                        t.cancel()
                    return

                ordered[idx] = result
                done_count += 1
                filename = result["original"].get("filename", "")

                # progress event
                prog = json.dumps({"done": done_count, "total": total, "filename": filename})
                yield f"event: progress\ndata: {prog}\n\n"

                # result event
                res_payload = json.dumps({"index": idx, "match": result})
                yield f"event: result\ndata: {res_payload}\n\n"

            matched_count = sum(1 for r in ordered if r and r.get("match"))
            done_payload = json.dumps({"matched": matched_count, "total": total})
            yield f"event: done\ndata: {done_payload}\n\n"
        finally:
            # Persist whatever matched (even on disconnect/timeout) in one write.
            _match_cache_flush(cache_updates)

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


# ─── Preview-Paths Endpoint ──────────────────────────────────────────

# Matches {placeholder} tokens in a naming template (same pattern the formatter
# uses), so unknown variables can be flagged against TEMPLATE_VARS.
_TEMPLATE_TOKEN_RE = re.compile(r'\{(\w+)\}')


class PreviewPathsRequest(BaseModel):
    """Request body for /api/preview-paths."""
    operations: list[dict]


@app.post("/api/preview-paths")
async def preview_paths(req: PreviewPathsRequest):
    """
    Lightweight template validation — no filesystem I/O.

    Runs build_new_path() for the first 5 operations and returns the resulting
    paths together with diagnostic flags so the client can detect degenerate
    templates (destination equals source, or effectively empty filename) before
    committing to a full rename pass.
    """
    results = []
    for operation in req.operations[:5]:  # cap at 5 — this is a quick sanity check
        raw_old = operation.get("old_path", "")
        old_path   = Path(raw_old)

        # Security: validate against allowed roots even for a preview,
        # so the endpoint cannot be used to probe arbitrary path strings.
        if not _is_allowed_path(old_path):
            raise HTTPException(status_code=403, detail="Path not in an allowed media directory")

        scene_data = operation.get("scene_data", {})
        file_data  = operation.get("file_data", {})
        tmpl       = operation.get("template", TEMPLATES["site_date"])
        flat       = operation.get("flat", False)

        bindings = extract_template_vars(
            scene_data, file_data, operation.get("performer_limit"))
        new_path = build_new_path(old_path, tmpl, bindings)
        if flat:
            new_path = old_path.parent / new_path.name

        # Detect {placeholders} the formatter can't resolve — they silently
        # render to empty (formatter.apply_template), so the UI warns about them
        # before a rename rather than after.  Validated against TEMPLATE_VARS,
        # the formatter's own canonical list, so this can never drift.
        unknown_vars = sorted(set(_TEMPLATE_TOKEN_RE.findall(tmpl)) - TEMPLATE_VARS)

        results.append({
            "old_path":       str(old_path),
            "new_path":       str(new_path),
            # True when the template produced no effective change
            "same_as_source": new_path.resolve() == old_path.resolve(),
            # True when the generated stem is blank / dot / dotdot
            "degenerate":     new_path.stem in ("", ".", ".."),
            # Placeholder names not recognised by the formatter (render to empty)
            "unknown_vars":   unknown_vars,
        })

    return {"previews": results}


# ─── Rename Endpoint ───────────────────────────────────────────────────

@app.post("/api/rename")
async def rename_files(req: RenameRequest, background_tasks: BackgroundTasks):
    """
    Execute rename operations.

    Phase 1 (filesystem moves/copies/hardlinks) runs synchronously and the
    results are returned immediately.  Phase 2 (metadata embedding + NFO
    writing) is dispatched as a FastAPI BackgroundTask so that slow FFmpeg
    operations on large files never block the HTTP response.  The client can
    track Phase 2 progress by polling ``/api/embed-status/{job_id}``.
    """
    action = RenameAction(req.action)

    # ── Phase 1: filesystem operations (sequential to avoid path conflicts) ──
    phase1 = []          # list of (result, new_path, meta, cat) tuples
    history_batch = []   # (old, new, action, success) — written in ONE save below
    confirm_updates = {} # oshash -> confirmed cache entry, flushed once below (D3)
    alias_learn_jobs = []  # (file_performers, api_performers) per confirm (F12)
    fp_submissions = 0     # per-request cap on opt-in StashDB contributions (F5)
    reserved: set[str] = set()  # targets claimed by THIS batch (collision policy, F1)
    for operation in req.operations:
        old_path   = Path(operation["old_path"])
        scene_data = operation.get("scene_data", {})
        file_data  = operation.get("file_data", {})
        tmpl       = operation.get("template", TEMPLATES["site_date"])
        flat       = operation.get("flat", False)

        bindings = extract_template_vars(
            scene_data, file_data, operation.get("performer_limit"))
        # F10: byte-budget truncation is reported so the preflight modal can
        # say "N names shortened" instead of the user discovering it post-hoc.
        path_report: dict = {}
        new_path = build_new_path(old_path, tmpl, bindings, report=path_report)
        if flat:
            new_path = old_path.parent / new_path.name

        # Collision policy (F1): resolve against the disk AND the targets this
        # batch already claimed, BEFORE touching the filesystem. Applies in
        # test mode too, so the preview shows the exact suffixed names.
        resolved_target, skip_code, collision_resolved = resolve_collision(
            old_path, new_path, req.on_conflict, reserved)
        if resolved_target is None:
            if skip_code == "target_exists":
                result = RenameResult(
                    success=False, old_path=old_path, new_path=new_path,
                    action=action, error=None, skipped="target_exists")
            else:  # no_free_suffix — a real error, every (2)..(99) slot taken
                result = RenameResult(
                    success=False, old_path=old_path, new_path=new_path,
                    action=action,
                    error=f"No free auto-number slot for: {new_path}")
            result.truncated = bool(path_report.get("truncated"))   # F10
            result.collision_resolved = False
            phase1.append((result, new_path, {}, None))
            continue
        new_path = resolved_target
        reserved.add(str(new_path))

        # Move the video AND its same-stem companions (subtitles / NFO / artwork)
        # with the same action, so a rename never orphans a `.srt`/`-poster.jpg`
        # next to the old name (F2). Companion results ride on result.companions.
        result = execute_rename_with_companions(old_path, new_path, action)
        # F10: preflight flags for the modal summary — the suffix policy changed
        # this name / a component was byte-budget shortened.
        result.collision_resolved = collision_resolved
        result.truncated = bool(path_report.get("truncated"))

        meta = {
            "title":        scene_data.get("title", ""),
            "site":         scene_data.get("site", ""),
            "performers":   scene_data.get("performers", []),
            "release_date": scene_data.get("release_date", ""),
            "tags":         scene_data.get("tags", []),
            # Scene poster URL (from the API match) — Phase 2 downloads it next to
            # the renamed file so the NFO can reference it (F3). Empty when the
            # scene has no poster.
            "poster_url":   scene_data.get("poster_url", ""),
            # NFO enrichment (F4): synopsis → <plot>, and provider id/source →
            # <uniqueid type="tpdb|stashdb">. The scene dict carries "source"
            # (set by _*_scene_to_dict); fall back to the request datasource.
            "description":  scene_data.get("description", ""),
            "id":           scene_data.get("id", ""),
            "source":       scene_data.get("source") or file_data.get("datasource") or "tpdb",
            # NFO enrichment (F5): probed duration (scan) falls back to the
            # API's scene duration; network → second <studio>; detector
            # quality/video_format → <streamdetails>.
            "duration_seconds": file_data.get("duration_seconds") or scene_data.get("duration"),
            "network":      scene_data.get("network", ""),
            "quality":      file_data.get("quality", ""),
            "video_format": file_data.get("video_format", ""),
            # NFO round three (F7): provider page link + fanart backdrop.
            "url":          scene_data.get("url", ""),
            "fanart_url":   scene_data.get("fanart_url", ""),
        }
        # Catalog payload for this op — applied in Phase 2 ONLY after the NFO is
        # actually written, so a file is never flagged "organized" (which the UI
        # reports as "has metadata + NFO") when the embed/NFO write failed or the
        # process died mid-embed. See _run_embed_phase / _embed_one.
        cat = None
        if result.success and action != RenameAction.TEST:
            # One group id per operation (F10): the video and every companion
            # it dragged along revert together, in one click.
            gid = uuid.uuid4().hex[:12]
            history_batch.append((old_path, new_path, action.value, True, None, gid))
            # Renaming a file is the user accepting its match → confirm it in the
            # cache (only when a real scene was attached). oshash is content-
            # stable, so the confirmation still hits after the move/rename.
            oshash = file_data.get("oshash")
            if oshash and scene_data.get("title"):
                confirm_updates[oshash] = _make_cache_entry(
                    scene_data, "confirmed", 100.0, confirmed=True)
                # A confirmed match is ground truth — queue it for alias
                # learning (F12): filename performers that fuzzy-miss every
                # scene performer may be TPDB-known aliases worth remembering.
                alias_learn_jobs.append((
                    file_data.get("performers") or [],
                    scene_data.get("performers") or [],
                ))
                # F6: StashDB per-scene credits carry alias↔canonical pairs
                # directly — seed them now, zero API calls (no-op for TPDB).
                _seed_credit_aliases(scene_data)
                # F5 (OPT-IN, default off): contribute this confirmed file's
                # fingerprints back to StashDB — capped per request.
                if fp_submissions < 20:
                    fp_submissions += _maybe_submit_fingerprints(scene_data, file_data)
                # F17: a confirmed rename whose filename site fuzzy-missed the
                # scene site IS the evidence for a site alias — learn it now.
                _f_site = str(file_data.get("site") or "").strip()
                _s_site = str(scene_data.get("site") or "").strip()
                if (_f_site and _s_site
                        and match_site(_f_site, _s_site, site_resolver=_site_lookup) < 0.9):
                    _site_alias_learn(_f_site, _s_site)
            cat = {
                "oshash":     oshash,
                "scene_id":   scene_data.get("id"),
                "source":     file_data.get("datasource"),
                "confidence": (file_data.get("confidence") or None),
                "confirmed":  bool(scene_data.get("title")),
                # Rename action — Phase 2 uses it to decide whether the OLD
                # cache key may be dropped after the embed re-hashes the file
                # (F6): "move" leaves no file with the old bytes; copy/hardlink
                # sources still carry them.
                "action":     action.value,
            }
            # The filesystem move already happened, so drop the stale source row
            # now (its content lives at new_path). Marking new_path "organized"
            # is deferred to Phase 2 (after the NFO write succeeds).
            try:
                if action == RenameAction.MOVE and str(new_path) != str(old_path):
                    catalog.forget(str(old_path))
            except Exception as e:
                print(f"WARNING: catalog rename integration failed: {e}")

            # Companion sidecars (F2): each successful companion move gets its own
            # history entry so it is independently revertible, and its stale
            # catalog row (subtitles are scanned/upserted too) is forgotten on MOVE.
            for comp in result.companions:
                if comp.success and comp.new_path:
                    history_batch.append(
                        (comp.old_path, comp.new_path, action.value, True, None, gid))
                    try:
                        if action == RenameAction.MOVE and str(comp.new_path) != str(comp.old_path):
                            catalog.forget(str(comp.old_path))
                    except Exception as e:
                        print(f"WARNING: catalog companion integration failed: {e}")

        phase1.append((result, new_path, meta, cat))

    # Persist all history entries for this request in a SINGLE disk write,
    # rather than one full-file rewrite per file (previously O(n²) per batch).
    if history_batch:
        history.add_entries(history_batch)
    if confirm_updates:
        _match_cache_flush(confirm_updates)
    if alias_learn_jobs:
        # Fire-and-forget: capped TPDB lookups, silently off without a key (F12).
        background_tasks.add_task(_learn_aliases, alias_learn_jobs)

    # Serialize Phase 1 results (embed_warning is null — Phase 2 not run yet)
    phase1_results = [
        {
            "success":       result.success,
            "old_path":      str(result.old_path),
            "new_path":      str(result.new_path) if result.new_path else None,
            "action":        result.action.value,
            "error":         result.error,
            # Collision-policy skip code (F1): "target_exists" renders as a
            # neutral ⏭ row, never as a red error.
            "skipped":       result.skipped,
            # Preflight flags (F10): the suffix policy renamed this target /
            # a component was shortened to the 255-byte budget.
            "collision_resolved": getattr(result, "collision_resolved", False),
            "truncated":          getattr(result, "truncated", False),
            "embed_warning": None,
            # Number of companion sidecars (subtitles/NFO/artwork) moved with this
            # file (F2) — the UI shows "+N companion file(s)" on the row.
            "companions_moved": sum(1 for c in result.companions if c.success),
        }
        for result, _, _, _ in phase1
    ]

    # Test mode: no embedding happens, skip the background task.
    if action == RenameAction.TEST:
        return {"results": phase1_results}

    # ── Phase 2: metadata embedding + NFO — runs in background ──────────────
    embeddable = [(r, p, m, c) for r, p, m, c in phase1 if r.success and r.new_path]
    job_id = uuid.uuid4().hex
    _job_create(job_id, len(embeddable), kind="embed")
    background_tasks.add_task(_run_embed_phase, job_id, embeddable, req.embed_mode)
    return {"results": phase1_results, "embed_job_id": job_id}


# Limit concurrent FFmpeg embed processes — NAS links saturate quickly with
# parallel I/O.  2 concurrent jobs keeps throughput high while avoiding timeouts.
# Lazy-initialised inside _run_embed_phase so it is always created inside the
# running event loop (safe across all Python 3.x versions).
_embed_sem: Optional[asyncio.Semaphore] = None


def _get_embed_sem() -> asyncio.Semaphore:
    global _embed_sem
    if _embed_sem is None:
        # 3 concurrent: FFmpeg now writes to local disk (_EMBED_STAGING_DIR),
        # so parallel jobs don't saturate NAS bandwidth during the encode pass.
        _embed_sem = asyncio.Semaphore(3)
    return _embed_sem


# Poster download (F3). Bounded concurrency so a large batch rename doesn't fire
# hundreds of simultaneous CDN GETs (the embed gather runs all files at once).
_POSTER_MAX_BYTES = 20 * 1024 * 1024   # 20 MB cap on a fetched poster
_poster_sem: Optional[asyncio.Semaphore] = None


def _get_poster_sem() -> asyncio.Semaphore:
    global _poster_sem
    if _poster_sem is None:
        _poster_sem = asyncio.Semaphore(4)
    return _poster_sem


async def _download_poster(url: str, dest: Path) -> bool:
    """Best-effort fetch of an image URL → write it to ``dest`` (<stem>-poster.jpg).

    This is a SERVER-SIDE fetch of a URL supplied by the metadata API, so it is
    hardened against SSRF/abuse even though the URL comes from a trusted source:
      • only ``http``/``https`` schemes;
      • redirects are NOT followed (blocks a redirect to an internal host);
      • the response must be an ``image/*`` Content-Type;
      • the body is size-capped (``_POSTER_MAX_BYTES``).
    Never raises and never partially writes a bad file — returns True only when a
    valid image was written. The 10 s timeout keeps a slow/unreachable CDN from
    stalling the background embed. ``dest`` must already be inside a path-validated
    media directory (the caller derives it from the just-renamed file).
    """
    if not url or not isinstance(url, str):
        return False
    if not (url.startswith("https://") or url.startswith("http://")):
        return False
    try:
        async with _get_poster_sem():
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                resp = await client.get(
                    url, headers={"User-Agent": "Adult-Media-Manager/1.0"}
                )
            if resp.status_code != 200:
                return False
            if not resp.headers.get("content-type", "").lower().startswith("image/"):
                return False
            data = resp.content
            if not data or len(data) > _POSTER_MAX_BYTES:
                return False
            dest.write_bytes(data)
            return True
    except Exception:
        return False


async def _refresh_fingerprint_after_embed(
    path: Path, old_oshash: Optional[str], *, delete_old: bool
) -> Optional[str]:
    """Recompute a file's oshash after a container write changed its bytes (F6).

    Every container strategy mutates the file (FFmpeg os.replace, AtomicParsley
    --overWrite, mkvpropedit header edit), so the match-cache entry that was
    confirmed under the PRE-embed hash would orphan itself on the next scan.
    Re-keys that entry to the new hash and refreshes the catalog row's
    oshash/size in place. ``delete_old`` drops the old key when no file can
    still carry the old bytes (rename action "move"); copy/hardlink sources and
    manual in-place saves keep it. Best-effort: returns the new hash or None.
    """
    try:
        new_oshash = await asyncio.to_thread(compute_oshash, path)
    except Exception:
        new_oshash = None
    if not new_oshash:
        return None
    _match_cache_rekey(old_oshash, new_oshash, delete_old=delete_old)
    try:
        catalog.update_fingerprint(str(path), new_oshash, path.stat().st_size)
    except Exception as e:
        print(f"WARNING: catalog fingerprint refresh failed: {e}")
    return new_oshash


async def _run_embed_phase(job_id: str, tasks: list, embed_mode: str = "embed") -> None:
    """
    Background task: write metadata for each successfully renamed file.

    embed_mode (see embedder.EMBED_MODES):
      "embed"      – remux tags into the container via FFmpeg, then write NFO.
      "smart"      – fast in-place tag edit (remux fallback), then write NFO.
      "nfo_only"   – write only the NFO sidecar (no container write).
      "embed_only" – container tags only (fast in-place), NO NFO sidecar.

    Two independent axes: a CONTAINER write (any mode except nfo_only) and a
    SIDECAR write (any mode except embed_only). FFmpeg/in-place work is serialised
    through _EMBED_SEM so NAS bandwidth isn't saturated. Updates _embed_jobs[job_id]
    as work completes so the client can poll progress.
    """
    job = _embed_jobs.get(job_id)
    if job is None:
        return

    embed_sem = _get_embed_sem()
    nfo_only   = embed_mode == "nfo_only"     # sidecar, no container
    # Container-only modes (no sidecar): in-place strategy or pure remux.
    embed_only = embed_mode in ("embed_only", "remux_only")

    async def _embed_one(result, new_path, meta, cat):
        warning = None
        new_oshash = None
        # Container write for embed/smart/embed_only (skipped for nfo_only). The
        # concurrency cap matters for the FFmpeg remux; nfo_only skips it entirely.
        if not nfo_only:
            async with embed_sem:
                ok, err = await _embed_for_mode(result.new_path, meta, embed_mode)
                if not ok:
                    warning = f"Metadata embedding warning: {err}"
                else:
                    # F6: the write changed the file's bytes — re-key the match
                    # cache + catalog to the post-embed hash so the confirmation
                    # made in Phase 1 survives the very embed that follows it.
                    new_oshash = await _refresh_fingerprint_after_embed(
                        result.new_path,
                        cat.get("oshash") if cat else None,
                        delete_old=bool(cat and cat.get("action") == "move"),
                    )
        nfo_written = False
        if not embed_only:  # write the sidecar for nfo_only/smart/embed
            # Poster (F3): only meaningful alongside the NFO that references it.
            if _FETCH_POSTERS and meta.get("poster_url"):
                poster_dest = result.new_path.with_name(result.new_path.stem + "-poster.jpg")
                if await _download_poster(meta["poster_url"], poster_dest):
                    meta = {**meta, "poster_path": poster_dest.name}
            try:
                write_nfo(result.new_path, meta)
                nfo_written = True
            except Exception as nfo_err:
                if not warning:
                    warning = f"NFO write warning: {nfo_err}"
        # Catalog (R1): "organised" is the NFO-on-disk signal (the scan self-heals
        # against it), so only mark it once the NFO is actually written. embed_only
        # writes container tags but no NFO, so it is intentionally NOT tracked as
        # organised — a re-scan would clear the flag anyway (no sidecar on disk).
        if cat and nfo_written:
            try:
                catalog.mark_organized(
                    str(result.new_path),
                    # Post-embed hash when the container was rewritten (F6);
                    # the pre-embed one otherwise (nfo_only — bytes untouched).
                    oshash=new_oshash or cat.get("oshash"),
                    scene_id=cat.get("scene_id"),
                    source=cat.get("source"),
                    confidence=cat.get("confidence"),
                    confirmed=cat.get("confirmed", False),
                )
            except Exception as e:
                print(f"WARNING: catalog mark_organized (phase 2) failed: {e}")
        _job_progress(
            job_id,
            {"path": str(result.new_path), "warning": warning} if warning else None,
        )

    await asyncio.gather(*[_embed_one(r, p, m, c) for r, p, m, c in tasks])
    _job_finish(job_id)


async def _run_manual_embed_job(
    job_id: str, file_path: Path, metadata: dict, embed_mode: str,
    old_oshash: Optional[str] = None,
) -> None:
    """
    Background container-embed for a single manual metadata save.

    The NFO sidecar is already written synchronously by the request handler, so
    this only performs the heavy FFmpeg remux (embed_mode "embed"/"smart").  It
    mirrors _run_embed_phase's job bookkeeping for ONE file and shares the same
    _embed_sem, so manual saves and rename Phase-2 embeds never saturate NAS
    bandwidth together.  Progress is polled via /api/embed-status/{job_id}.

    Identical on every build target — pure stdlib + the shared embed helpers,
    no platform-specific code.
    """
    job = _embed_jobs.get(job_id)
    if job is None:
        return

    warning = None
    try:
        async with _get_embed_sem():
            ok, err = await _embed_for_mode(file_path, metadata, embed_mode)
            if not ok:
                warning = f"Metadata embedding warning: {err}"
            else:
                # F6: the embed changed the file's bytes — re-key the confirm
                # the request handler stored under the pre-embed hash. Old key
                # kept: an in-place edit has no action context, and an unknown
                # hardlink/copy elsewhere may still carry the old bytes.
                await _refresh_fingerprint_after_embed(
                    file_path, old_oshash, delete_old=False)
    except Exception as e:  # never let a background crash leave the job stuck
        warning = f"Metadata embedding warning: {e}"

    _job_progress(
        job_id, {"path": str(file_path), "warning": warning} if warning else None
    )
    _job_finish(job_id)


# ─── Embed-status Endpoint ─────────────────────────────────────────────

@app.get("/api/embed-status/{job_id}")
async def embed_status(job_id: str):
    """
    Poll Phase-2 (metadata embedding) progress for a rename job.

    Returns:
        job_id    – echoed back for client convenience
        total     – number of files queued for embedding
        done      – number of files whose embedding has completed (success or warning)
        complete  – true once all files have been processed
        warnings  – list of {path, warning} for files where embedding/NFO failed

    The record is kept for EMBED_JOB_TTL seconds then discarded; polling
    after expiry returns 404.
    """
    # Validate job_id to be a 32-char hex string (uuid4().hex) — no path traversal
    if not job_id.isalnum() or len(job_id) != 32:
        raise HTTPException(status_code=400, detail="Invalid job_id")

    # Prune stale entries (TTL-based; cheap O(n) scan on a tiny dict)
    now = _time.monotonic()
    stale = [k for k, v in _embed_jobs.items() if now - v["created"] > EMBED_JOB_TTL]
    for k in stale:
        _embed_jobs.pop(k, None)

    # Live, in-process job is authoritative while the process is alive.
    job = _embed_jobs.get(job_id)
    if job is not None:
        return {
            "job_id":   job_id,
            "total":    job["total"],
            "done":     job["done"],
            "complete": job["complete"],
            "warnings": job["warnings"],
            "status":   "complete" if job["complete"] else "running",
        }

    # Fallback (review item R2): not in memory — either the page was refreshed and
    # we're re-attaching, or the server restarted. The durable store returns the
    # last-known state; a job left "running" at the last shutdown was flipped to
    # "interrupted" at startup, so the client gets a clear terminal state instead
    # of a 404 that would hang or silently drop the progress banner.
    stored = _job_store.get(job_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Embed job not found or expired")
    return {
        "job_id":   job_id,
        "total":    stored["total"],
        "done":     stored["done"],
        "complete": stored["complete"],
        "warnings": stored["warnings"],
        "status":   stored["status"],
    }


# ─── Settings Endpoints ────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    """
    Return the active/source status of each configurable API key.

    Key VALUES are never exposed — only whether each key is currently active
    and where it came from ('env', 'settings', or null).  This lets the UI
    display accurate badge state without leaking secrets to the browser.
    """
    _, tpdb_src    = _effective_key("TPDB_API_KEY",    "tpdb_api_key")
    _, stashdb_src = _effective_key("STASHDB_API_KEY", "stashdb_api_key")
    return {
        "tpdb":    {"active": tpdb    is not None, "source": tpdb_src},
        "stashdb": {"active": stashdb is not None, "source": stashdb_src},
        # Non-secret UI preferences — safe to expose so a fresh browser/profile
        # can pick up the server-saved choice.
        "locale":  _effective_locale(),
        "theme":   _effective_theme(),
        "embed_mode": _effective_embed_mode(),
        "performer_order": _effective_performer_order(),
        # F5 — opt-in, DEFAULT OFF: only an explicit stored True enables it.
        "contribute_fingerprints": _load_settings().get("contribute_fingerprints") is True,
    }


class SaveSettingsRequest(BaseModel):
    tpdb_api_key:    Optional[str] = None
    stashdb_api_key: Optional[str] = None
    locale:          Optional[str] = None
    theme:           Optional[str] = None
    embed_mode:      Optional[str] = None
    performer_order: Optional[str] = None
    # F5 — OPT-IN fingerprint contribution to StashDB. Tri-state on the wire:
    # None = leave unchanged, True/False = set explicitly (the generic truthy
    # prefs loop would treat False as "keep", silently making the opt-in
    # impossible to turn OFF — handled separately in save_settings).
    contribute_fingerprints: Optional[bool] = None
    # Explicit key removal (roadmap-2 F14). Blank still means "keep" — clearing
    # must be a deliberate, separate signal so an empty form can never wipe a
    # key by accident. Ignored for env-sourced keys (same precedence as writes).
    clear_tpdb:      bool = False
    clear_stashdb:   bool = False

    @field_validator("tpdb_api_key", "stashdb_api_key", mode="before")
    @classmethod
    def _strip_and_cap(cls, v) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        if len(v) > _SETTINGS_KEY_MAX_LEN:
            raise ValueError(f"API key exceeds maximum length ({_SETTINGS_KEY_MAX_LEN})")
        return v or None   # normalise empty string → None

    @field_validator("locale", mode="before")
    @classmethod
    def _validate_locale(cls, v) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip().lower()
        if not v:
            return None
        if v not in _ALLOWED_LOCALES:
            raise ValueError(f"Unsupported locale: {v}. Allowed: {sorted(_ALLOWED_LOCALES)}")
        return v

    @field_validator("theme", mode="before")
    @classmethod
    def _validate_theme(cls, v) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip().lower()
        if not v:
            return None
        if v not in _ALLOWED_THEMES:
            raise ValueError(f"Unsupported theme: {v}. Allowed: {sorted(_ALLOWED_THEMES)}")
        return v

    @field_validator("embed_mode", mode="before")
    @classmethod
    def _validate_embed_mode_pref(cls, v) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        return _validate_embed_mode(v)   # raises on anything outside EMBED_MODES

    @field_validator("performer_order", mode="before")
    @classmethod
    def _validate_performer_order(cls, v) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip().lower()
        if not v:
            return None
        if v not in _ALLOWED_PERFORMER_ORDERS:
            raise ValueError(
                f"Unsupported performer order: {v}. Allowed: {sorted(_ALLOWED_PERFORMER_ORDERS)}")
        return v


@app.post("/api/settings")
async def save_settings(req: SaveSettingsRequest):
    """
    Persist API keys that were not provided via environment variables.

    Rules:
    • If a key is set in the environment it is immutable — the UI cannot
      override env-supplied secrets (source: 'env' in GET /api/settings).
    • A blank / missing field means "keep whatever is already saved".
    • After saving, both API clients are hot-reloaded in-memory so the new
      keys take effect immediately without a container restart.
    """
    global tpdb, stashdb

    settings = _load_settings()
    changed  = []          # API-key labels saved — surfaced in the UI toast
    removed  = []          # API-key labels cleared (F14) — separate toast
    dirty    = False       # whether anything (keys or prefs) needs persisting

    for field_name, env_var, settings_key, label, clear_field in [
        ("tpdb_api_key",    "TPDB_API_KEY",    "tpdb_api_key",    "TPDB",    "clear_tpdb"),
        ("stashdb_api_key", "STASHDB_API_KEY", "stashdb_api_key", "StashDB", "clear_stashdb"),
    ]:
        new_val = getattr(req, field_name)

        # Env var takes precedence — silently ignore UI attempts to change it.
        if os.getenv(env_var, "").strip():
            continue

        # Explicit clear (F14) wins over any pasted value in the same request —
        # removal is the deliberate action; a stray input must not survive it.
        if getattr(req, clear_field):
            if settings.pop(settings_key, None) is not None:
                removed.append(label)
                dirty = True
            continue

        if new_val:          # non-empty → update saved value
            settings[settings_key] = new_val
            changed.append(label)
            dirty = True
        # blank → leave existing saved value untouched

    # UI preferences (already whitelist-validated above). A blank/None value
    # means "leave unchanged"; only persist when it actually differs.
    for pref in ("locale", "theme", "embed_mode", "performer_order"):
        new_val = getattr(req, pref)
        if new_val and settings.get(pref) != new_val:
            settings[pref] = new_val
            dirty = True

    # F5: explicit tri-state boolean — False is a REAL value (turn the opt-in
    # off), so it can't ride the truthy prefs loop above.
    if (req.contribute_fingerprints is not None
            and settings.get("contribute_fingerprints") != req.contribute_fingerprints):
        settings["contribute_fingerprints"] = req.contribute_fingerprints
        dirty = True

    if dirty:
        _save_settings(settings)

    # Hot-reload clients regardless of whether anything changed, so that a
    # previously failing client (wrong key) can recover after correction.
    old_tpdb    = tpdb
    old_stashdb = stashdb
    tpdb    = _init_tpdb()
    stashdb = _init_stashdb()

    # Close old HTTP sessions gracefully (fire-and-forget; ignore errors).
    for old_client in (old_tpdb, old_stashdb):
        if old_client is not None:
            try:
                await old_client.close()
            except Exception:
                pass

    _, tpdb_src    = _effective_key("TPDB_API_KEY",    "tpdb_api_key")
    _, stashdb_src = _effective_key("STASHDB_API_KEY", "stashdb_api_key")
    return {
        "ok":      True,
        "changed": changed,
        "removed": removed,
        "tpdb":    {"active": tpdb    is not None, "source": tpdb_src},
        "stashdb": {"active": stashdb is not None, "source": stashdb_src},
        "locale":  _effective_locale(),
        "theme":   _effective_theme(),
        "embed_mode": _effective_embed_mode(),
        "performer_order": _effective_performer_order(),
        "contribute_fingerprints": _load_settings().get("contribute_fingerprints") is True,
    }


# ─── History Endpoints ─────────────────────────────────────────────────

@app.get("/api/history")
async def get_history(limit: int = Query(50, ge=1, le=500)):
    """
    Get rename history.
    """
    entries = history.get_recent(limit)
    # Group bookkeeping (F10): per group, how many members and which entry is
    # the primary (the video — first appended). Computed over the FULL history
    # so a group straddling the `limit` window still counts correctly.
    group_counts: dict[str, int] = {}
    group_primary: dict[str, str] = {}
    for e in history.entries:
        gid = e.group_id
        if gid and e.success and e.action in ("move", "copy", "hardlink", "symlink"):
            group_counts[gid] = group_counts.get(gid, 0) + 1
            group_primary.setdefault(gid, e.id)
    return {
        "entries": [
            {
                "id": e.id,
                "timestamp": e.timestamp,
                "old_path": e.old_path,
                "new_path": e.new_path,
                "action": e.action,
                "success": e.success,
                "error": e.error,
                # Whether this row can be reverted (action + success). The actual
                # filesystem state is checked at revert time, so we don't stat
                # every path here (avoids 50 NAS stat calls per history open).
                "revertible": history.is_revertible(e),
                # Grouped rename info (F10): the UI folds companion rows into a
                # "+N companions" chip on the primary (video) row.
                "group_id": e.group_id,
                "group_primary": bool(e.group_id) and group_primary.get(e.group_id) == e.id,
                "companions": max(0, group_counts.get(e.group_id, 1) - 1) if e.group_id else 0,
            }
            for e in entries
        ]
    }


@app.post("/api/history/undo")
async def undo_rename():
    """
    Undo the last rename operation (most recent revertible *move*).
    Paths are confined to the allowed roots before any filesystem change.
    """
    entry, group_results = history.undo_last(is_allowed=_is_allowed_path)
    if entry:
        resp = {
            "success": True,
            "undone": {
                "old_path": entry.old_path,
                "new_path": entry.new_path,
            },
        }
        # Grouped rename (F10): report every member's outcome so the client can
        # say "3 files restored" instead of pretending it was one.
        if group_results is not None:
            resp["group"] = [
                {"new_path": p, "ok": ok, "code": code}
                for p, ok, code in group_results
            ]
        return resp
    else:
        return {
            "success": False,
            "error": "No operations to undo"
        }


class RevertRequest(BaseModel):
    """Request body for /api/history/revert."""
    id: str


@app.post("/api/history/revert")
async def revert_history_entry(req: RevertRequest):
    """
    Revert a single history entry by id.

    A "move" is undone by moving the file back; "copy"/"hardlink"/"symlink" are
    undone by deleting the created file/link (the original is left untouched).
    Every path is validated against the allowed roots before any change.

    NOTE: embedded container metadata and NFO sidecars written during the rename
    are NOT removed — that step is not reversible. The UI states this.

    Returns {"success": bool, "code": str, "reverted"?: {...}} so the client can
    localise the outcome (codes: ok, not_revertible, already_reverted,
    source_exists, forbidden, error).
    """
    entry = history.get_entry(req.id)
    if entry is None:
        raise HTTPException(status_code=404, detail="History entry not found")

    # Grouped rename (F10): reverting any member restores the WHOLE set —
    # video first, then companions — with per-file outcome codes.
    if entry.group_id:
        results = history.revert_group(entry.group_id, is_allowed=_is_allowed_path)
        any_ok = any(ok for _, ok, _ in results)
        # Representative code for the toast: ok when anything moved back,
        # otherwise the (common) blocking code, e.g. already_reverted.
        codes = [code for _, ok, code in results if not ok]
        code = "ok" if any_ok else (codes[0] if codes else "error")
        return {
            "success": any_ok,
            "code": code,
            "group": [
                {"new_path": p, "ok": ok, "code": c} for p, ok, c in results
            ],
        }

    ok, code = history.revert_entry(entry, is_allowed=_is_allowed_path)
    resp: dict = {"success": ok, "code": code}
    if ok:
        resp["reverted"] = {
            "old_path": entry.old_path,
            "new_path": entry.new_path,
            "action":   entry.action,
        }
    return resp


# ─── Browse Endpoint ───────────────────────────────────────────────────

# Default starting directory for the browse modal.
#   • Native (deb/AppImage): the user's home (AMM_HOME, set by Electron).
#   • Docker: "/" — resolved to the *virtual roots view* below (the list of
#     allowed mount points that exist), so users land on a picker of their
#     mounted folders instead of a hard-coded "/media" that may not be mounted.
_BROWSE_DEFAULT = os.getenv("AMM_HOME", os.path.expanduser("~")) if _AMM_NATIVE else "/"


def _existing_allowed_roots() -> list[Path]:
    """Allowed roots that currently exist as directories (Docker roots view).

    Best-effort and never raises — a dead/hung mount among ALLOWED_ROOTS must not
    break the whole picker, so per-root stat errors are swallowed.
    """
    found: list[Path] = []
    for r in ALLOWED_ROOTS:
        try:
            if r.is_dir():
                found.append(r)
        except OSError:
            continue
    return sorted(found, key=lambda p: str(p))


def _read_mount_points() -> Optional[list[Path]]:
    """Return every mount target from the container's mount table.

    Reads /proc/self/mountinfo (Linux/Docker). The mount-point field escapes
    special characters (space→\\040, tab→\\011, newline→\\012, backslash→\\134);
    we unescape them so paths with spaces resolve correctly. Returns None when
    the mount table can't be read (e.g. a non-Linux dev host) so callers can
    fall back to the static allowlist.
    """
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None

    points: list[Path] = []
    for line in lines:
        # Fields: mount_id parent_id major:minor root MOUNT_POINT options …
        # The mount point is always the 5th field, before the optional fields.
        parts = line.split(" ")
        if len(parts) < 5:
            continue
        mp = (parts[4]
              .replace("\\040", " ")
              .replace("\\011", "\t")
              .replace("\\012", "\n")
              .replace("\\134", "\\"))
        points.append(Path(mp))
    return points


def _browsable_roots() -> list[Path]:
    """Top-level entries for the Docker "/" virtual roots view.

    Lists only the media locations the user actually mounted into the container
    — derived from the real mount table and confined to ALLOWED_ROOTS — instead
    of every built-in root that merely exists as an (often empty) directory in
    the base image. So a user who mounts a single ``/mnt/NAS`` share sees just
    that, not a noisy /data /home /media /mnt /root /srv list.

    Falls back to the existing-allowed-roots behaviour when the mount table is
    unavailable or no media mount is detected, so the picker is never an empty
    dead-end (the user can still type a path or set AMM_EXTRA_ROOTS).
    """
    mounts = _read_mount_points()
    if mounts is None:
        return _existing_allowed_roots()

    data_dir = DATA_DIR.resolve()
    roots: set[Path] = set()
    for mp in mounts:
        try:
            rp = mp.resolve()
        except OSError:
            continue
        # Skip the filesystem root and the app's own persistent data volume —
        # neither is a media location the user wants to browse.
        if rp == Path("/") or rp == data_dir:
            continue
        # Confine to the allowlist (built-in roots + AMM_EXTRA_ROOTS); this also
        # drops system bind-mounts like /etc/hosts, /etc/resolv.conf, /proc, …
        if not _is_allowed_path(rp):
            continue
        try:
            if not rp.is_dir():
                continue
        except OSError:
            continue
        roots.add(rp)

    if not roots:
        return _existing_allowed_roots()
    return sorted(roots, key=lambda p: str(p))


@app.get("/api/browse")
def browse_directory(path: str = Query(None), show_hidden: bool = Query(False)):
    """
    Browse directories on the server.
    Returns list of subdirectories and files.

    Hidden entries (dot-files like .ssh, .cache, .config) are omitted unless
    ``show_hidden=true`` — this matches the scanner's dot-file policy and keeps
    the picker focused on real media instead of home-directory clutter. The
    ".." parent link is always shown.

    Plain ``def`` (see scan_directory): the iterdir()/stat() calls are blocking
    filesystem I/O with no awaits, so FastAPI runs this in its worker threadpool
    to keep the event loop responsive on slow mounts.
    """
    if path is None:
        path = _BROWSE_DEFAULT
    try:
        p = Path(path).resolve()

        # ── Virtual roots view (Docker) ─────────────────────────────────────
        # "/" is deliberately NOT an allowed root (we must never expose the whole
        # host filesystem), but the user still needs a top level to pick their
        # mounted folders from — otherwise browsing dead-ends at a 403 on "/" and
        # mounts like /mnt are only reachable by typing the path. So browsing "/"
        # returns ONLY the allowed roots that exist (the configured mounts). This
        # exposes nothing beyond what the allowlist already permits.
        # Native mode (AMM_NATIVE=1) skips this and lists "/" for real.
        if p == Path("/") and not _AMM_NATIVE:
            return {
                "path": "/",
                "is_root": True,
                "items": [
                    {"name": str(r), "path": str(r), "type": "directory", "size": 0}
                    for r in _browsable_roots()
                ],
            }

        # Security: reject anything outside the explicitly allowed mount points.
        # Native mode (AMM_NATIVE=1) allows any absolute path.
        if not _is_allowed_path(p):
            raise HTTPException(status_code=403, detail="Access denied to this path")

        if not p.exists():
            raise HTTPException(status_code=404, detail="Path does not exist")

        if not p.is_dir():
            raise HTTPException(status_code=400, detail="Path is not a directory")

        items = []

        # Parent (".." ) link. When the literal parent isn't browsable in Docker
        # (e.g. the parent of an allowed root, or a non-allowed ancestor like
        # /run for /run/media), point ".." at the virtual roots view ("/") so the
        # user can hop between mounts instead of hitting a 403.
        parent = p.parent
        if p == parent:
            parent_link = None                       # already at filesystem root
        elif _AMM_NATIVE or _is_allowed_path(parent):
            parent_link = str(parent)
        else:
            parent_link = "/"                        # → virtual roots view
        if parent_link is not None:
            items.append({
                "name": "..",
                "path": parent_link,
                "type": "directory",
                "size": 0,
            })

        # List directory contents. iterdir() on the directory itself can raise
        # PermissionError/OSError (e.g. /root is in the allowlist but mode 700
        # and the process isn't root, or a dead NAS mount) — surface that as a
        # clean 403/503 instead of a generic 500 from the catch-all below.
        try:
            entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            raise HTTPException(status_code=403, detail="Permission denied for this directory")
        except OSError as e:
            raise HTTPException(status_code=503, detail=f"Cannot read directory: {e}")

        for item in entries:
            # Skip dot-files/dirs unless explicitly requested (UI "Show hidden").
            if not show_hidden and item.name.startswith('.'):
                continue
            try:
                items.append({
                    "name": item.name,
                    "path": str(item),
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else 0,
                })
            except (PermissionError, OSError):
                continue

        return {
            "path": str(p),
            "items": items,
            "show_hidden": show_hidden,
        }

    except HTTPException:
        raise  # pass 403/404/400 through unchanged
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Templates Endpoint ────────────────────────────────────────────────

@app.get("/api/templates")
async def get_templates():
    """
    Get available naming templates and the list of valid {placeholder} variables
    (so the UI can validate custom templates against the same canonical set the
    formatter uses).
    """
    return {"templates": TEMPLATES, "variables": sorted(TEMPLATE_VARS)}


# ─── Manual Editing & Thumbnails ───────────────────────────────────────

# Curated list of common adult content tags/genres
CURATED_TAGS = sorted([
    "69", "Amateur", "Anal", "Asian", "Ass Licking", "BDSM", "Big Ass",
    "Big Dick", "Big Tits", "Bikini", "Blonde", "Blowjob", "Bondage",
    "Brunette", "Casting", "Cheating", "Chubby", "Compilation", "Cougar",
    "Creampie", "Cumshot", "Curvy", "Deep Throat", "Dildo", "Dominant",
    "Double Penetration", "Ebony", "European", "Facesitting", "Facial",
    "Femdom", "Fetish", "Fingering", "Footjob", "Gangbang", "Glasses",
    "Granny", "Group Sex", "Handjob", "Hardcore", "High Heels", "Interracial",
    "Kissing", "Latex", "Latina", "Lesbian", "Lingerie", "Masturbation",
    "Mature", "MILF", "Natural Tits", "Nurse", "Office", "Oral",
    "Orgasm", "Orgy", "Outdoor", "Petite", "Piercing", "POV",
    "Public", "Redhead", "Rimjob", "Roleplay", "Secretary", "Sensual",
    "Skinny", "Small Tits", "Solo", "Softcore", "Squirting", "Step Fantasy",
    "Stockings", "Strapon", "Strip", "Submissive", "Taboo", "Tattoo",
    "Teen (18+)", "Threesome (FFM)", "Threesome (MMF)", "Titjob", "Toy",
    "Uniform", "Vibrator",
])


class _JsonStore:
    """Thread-safe, in-memory-cached JSON file store with atomic writes.

    Replaces the old per-call ``_load_*``/``_save_*`` helpers that re-read and
    re-parsed the file on every request and rewrote the whole file on every
    change (the latter called from inside the concurrent match ``gather``).

    Properties:
      • The value is loaded once and cached, so request paths and match tasks
        no longer do blocking file reads on the event loop.
      • A single lock serialises every read-modify-write, so neither FastAPI's
        worker threadpool (sync endpoints) nor a future multi-thread setup can
        interleave an update or observe a half-written file.
      • Writes go through a temp file + ``os.replace`` so a crash can never
        leave a truncated/corrupt JSON file.
    Identical behaviour on every build target — pure stdlib, no platform code.
    """

    def __init__(self, path: Path, default_factory):
        self._path = path
        self._default_factory = default_factory   # callable -> fresh default
        self._lock = threading.RLock()
        self._cache = None                         # lazy-loaded

    def _ensure_loaded_locked(self):
        if self._cache is not None:
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        default = self._default_factory()
        self._cache = data if isinstance(data, type(default)) else default

    def get(self):
        """Return a deep copy of the value (callers may freely mutate it)."""
        with self._lock:
            self._ensure_loaded_locked()
            return copy.deepcopy(self._cache)

    def get_key(self, key):
        """Return a deep copy of a single dict entry, or None.

        For dict-backed stores only. Avoids deep-copying the whole store on every
        lookup (important for a large match cache queried once per file).
        """
        with self._lock:
            self._ensure_loaded_locked()
            if isinstance(self._cache, dict):
                val = self._cache.get(key)
                return copy.deepcopy(val) if val is not None else None
            return None

    def mutate(self, fn) -> bool:
        """
        Run ``fn(cache)`` under the lock, mutating the cached object in place.
        If ``fn`` returns True the cache is persisted atomically.  ``fn`` may
        raise to reject the change.  Returns whether a write happened.
        """
        with self._lock:
            self._ensure_loaded_locked()
            changed = fn(self._cache)
            if changed:
                self._write_locked()
            return bool(changed)

    def _write_locked(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(self._path) + ".tmp")
        tmp.write_text(json.dumps(self._cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)


USER_TAGS_FILE = DATA_DIR / "user_tags.json"
_user_tags_store = _JsonStore(USER_TAGS_FILE, list)

def _load_user_tags() -> list[str]:
    return _user_tags_store.get()

@app.get("/api/tags")
async def get_tags():
    """Return curated + user-created tags/genres, merged and sorted."""
    user_tags = _load_user_tags()
    merged = sorted(set(CURATED_TAGS) | set(user_tags))
    return {"tags": merged, "user_tags": user_tags}

_MAX_USER_TAGS = 500
_MAX_TAG_LEN   = 100


class AddTagRequest(BaseModel):
    tag: str

    @field_validator("tag")
    @classmethod
    def _validate_tag(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Tag cannot be empty")
        if len(v) > _MAX_TAG_LEN:
            raise ValueError(f"Tag exceeds {_MAX_TAG_LEN} character limit")
        return v


@app.post("/api/tags")
async def add_user_tag(req: AddTagRequest):
    """Save a new user-created tag persistently."""
    class _LimitReached(Exception):
        pass

    def _add(tags: list) -> bool:
        if req.tag in tags:
            return False
        if len(tags) >= _MAX_USER_TAGS:
            raise _LimitReached()
        tags.append(req.tag)
        tags.sort()
        return True

    try:
        _user_tags_store.mutate(_add)
    except _LimitReached:
        raise HTTPException(status_code=400, detail=f"Tag limit ({_MAX_USER_TAGS}) reached")
    return {"ok": True, "tag": req.tag}

@app.delete("/api/tags/{tag}")
async def delete_user_tag(tag: str):
    """Remove a user-created tag."""
    def _delete(tags: list) -> bool:
        if tag not in tags:
            return False
        tags[:] = [t for t in tags if t != tag]   # mutate in place
        return True

    _user_tags_store.mutate(_delete)
    return {"ok": True}

KNOWN_SITES_FILE = DATA_DIR / "known_sites.json"
_known_sites_store = _JsonStore(KNOWN_SITES_FILE, list)

def _load_known_sites() -> list[dict]:
    return _known_sites_store.get()

def _add_known_site(name: str, network: str = "") -> None:
    """Register a single site if not already known (cached + atomic write).

    Safe to call from inside the concurrent match gather: the cached read means
    no per-call file I/O, and the lock + atomic write make the update safe.
    """
    if not name:
        return

    def _add(sites: list) -> bool:
        if any(s.get("name") == name for s in sites):
            return False
        sites.append({"name": name, "network": network})
        sites.sort(key=lambda s: s["name"].lower())
        _invalidate_site_index()   # F17: resolver index rebuilds lazily
        return True

    _known_sites_store.mutate(_add)

@app.get("/api/search-sites")
async def search_sites(q: str = Query(default="", min_length=0)):
    """Search TPDB for site/studio names. Empty q returns cached known sites."""
    q = q.strip()
    if not q:
        # Return locally known sites when field is first focused
        known = _load_known_sites()
        return {"sites": known}
    if not tpdb:
        raise HTTPException(status_code=503, detail="TPDB not configured")
    results = await tpdb.search_sites(q)
    sites = [{"id": s.id, "name": s.name, "network": s.network} for s in results]

    # Cache any new ones found in a SINGLE locked, atomic write (was one full
    # read-modify-write per result).  s.network is str|None from _parse_site().
    def _add_all(known: list) -> bool:
        existing = {s.get("name") for s in known}
        added = False
        for s in results:
            if s.name and s.name not in existing:
                known.append({"name": s.name, "network": s.network or ""})
                existing.add(s.name)
                added = True
        if added:
            known.sort(key=lambda x: x["name"].lower())
            _invalidate_site_index()   # F17
        return added

    _known_sites_store.mutate(_add_all)
    return {"sites": sites}


# ─── Persistent match cache / canonical-ID catalog (review item D3) ───────────
# Maps a file's content fingerprint (oshash, computed at scan) to its matched
# scene, so a re-scan/re-match reuses the result instead of recomputing pHash and
# re-querying the API (saving the most expensive work and API rate limit). Keyed
# by oshash, the entry survives renames/moves (content-stable). Entries:
#   { "<oshash>": {scene, source, confidence, match_method, user_confirmed,
#                  updated_at} }
# Stored as plain JSON in the already-secured DATA_DIR (same place as history),
# bounded so it can't grow without limit. No secrets, no platform code — behaves
# identically on Docker/deb/AppImage.
_MATCH_CACHE_FILE = DATA_DIR / "match_cache.json"
_match_cache_store = _JsonStore(_MATCH_CACHE_FILE, dict)
try:
    _MATCH_CACHE_MAX = int(os.getenv("AMM_MATCH_CACHE_MAX", "50000"))
except ValueError:
    _MATCH_CACHE_MAX = 50000
if _MATCH_CACHE_MAX < 0:
    _MATCH_CACHE_MAX = 0   # 0 = unbounded


def _make_cache_entry(scene: dict, source: str, confidence, match_method=None,
                      confirmed: bool = False, coverage=None,
                      match_fields=None) -> dict:
    return {
        "scene": scene,
        "source": source,
        "confidence": confidence,
        "match_method": match_method,
        "user_confirmed": confirmed,
        # Evidence coverage + fields (D7) so a cache hit shows the same "based on
        # …" note as a fresh match. Absent on legacy entries → handled on read.
        "coverage": coverage,
        "match_fields": match_fields or [],
        "updated_at": _time.time(),
    }


def _bound_match_cache(cache: dict) -> None:
    """Evict the oldest NON-confirmed entries until within the cap (in place)."""
    cap = _MATCH_CACHE_MAX
    if not cap or len(cache) <= cap:
        return
    evictable = sorted(
        (k for k, v in cache.items() if not v.get("user_confirmed")),
        key=lambda k: cache[k].get("updated_at", 0),
    )
    i = 0
    while len(cache) > cap and i < len(evictable):
        del cache[evictable[i]]
        i += 1


def _match_cache_lookup(oshash: str, file_data: dict) -> Optional[dict]:
    """Return a match-result dict from the cache for this oshash, or None."""
    if not oshash:
        return None
    entry = _match_cache_store.get_key(oshash)
    if not entry or not entry.get("scene"):
        return None
    return {
        "original": file_data,
        "match": entry["scene"],
        "confidence": entry.get("confidence", 0),
        "alternatives": [],
        "match_method": "cache",
        "cached": True,
        "user_confirmed": bool(entry.get("user_confirmed")),
        "coverage": entry.get("coverage"),
        "match_fields": entry.get("match_fields", []),
    }


def _match_cache_flush(updates: dict) -> None:
    """Persist a batch of auto-match results in a SINGLE atomic write.

    Never downgrades a user-confirmed entry back to an auto one (keeps the
    confirmed flag, just refreshes the snapshot). Called once per match run.
    """
    if not updates:
        return

    def _apply(cache: dict) -> bool:
        for oshash, entry in updates.items():
            existing = cache.get(oshash)
            if existing and existing.get("user_confirmed"):
                entry = {**entry, "user_confirmed": True}
            cache[oshash] = entry
        _bound_match_cache(cache)
        return True

    _match_cache_store.mutate(_apply)


def _match_cache_confirm(oshash: str, scene: dict, source: str, confidence=100.0) -> None:
    """Mark a file's match as user-confirmed (manual edit / accepted rename).

    Best-effort: confirmed entries are trusted on the next scan and are never
    evicted by the size cap.
    """
    if not oshash or not scene:
        return

    def _apply(cache: dict) -> bool:
        cache[oshash] = _make_cache_entry(scene, source, confidence, confirmed=True)
        _bound_match_cache(cache)
        return True

    _match_cache_store.mutate(_apply)


def _match_cache_rekey(old_oshash: Optional[str], new_oshash: str, *,
                       delete_old: bool) -> None:
    """Duplicate a cache entry under a new content hash after an embed rewrote
    the file's bytes (F6), so fingerprint-keyed trust survives embedding.

    ``delete_old`` drops the old key when nothing can still carry the old bytes
    (rename action "move"); copy/hardlink sources and manual in-place saves
    keep it — a stale-but-unreachable entry is harmless, a wrongly deleted
    confirmation is not. No-op when the hashes match (e.g. an mkvpropedit edit
    that stayed outside the oshash head/tail windows) or the old key is absent.
    """
    if not old_oshash or not new_oshash or old_oshash == new_oshash:
        return

    def _apply(cache: dict) -> bool:
        entry = cache.get(old_oshash)
        if not entry:
            return False
        cache[new_oshash] = {**entry, "updated_at": _time.time()}
        if delete_old:
            del cache[old_oshash]
        _bound_match_cache(cache)
        return True

    _match_cache_store.mutate(_apply)


# ── Learned performer aliases (F12) ──────────────────────────────────────────
# {normalized alias -> canonical API name}, learned from user-confirmed renames
# whose filename credited a performer under a name TPDB knows only as an alias.
# Same _JsonStore pattern as the other DATA_DIR stores; injected into the pure
# matcher as a resolver callable, so scoring stays unit-testable and None-safe.
_PERFORMER_ALIASES_FILE = DATA_DIR / "performer_aliases.json"
_performer_aliases_store = _JsonStore(_PERFORMER_ALIASES_FILE, dict)

# Learned site aliases (F17): {normalized filename token: canonical site}.
# Taught by user-confirmed renames whose filename site fuzzy-missed the scene
# site — the confirmed pair IS the evidence, no API calls. Seeded once with
# the four abbreviations that used to be hardcoded in matcher.py, so behavior
# is a strict superset of the old dict.
_SITE_ALIASES_FILE = DATA_DIR / "site_aliases.json"
_site_aliases_store = _JsonStore(_SITE_ALIASES_FILE, dict)

_LEGACY_SITE_ABBREVIATIONS = {
    "bg": "Brazzers", "rk": "Reality Kings", "ts": "TeamSkeet", "bang": "BangBros",
}


def _seed_site_aliases() -> None:
    """Migrate the retired matcher.py abbreviation dict into the store (once)."""
    def _apply(cache: dict) -> bool:
        changed = False
        for k, v in _LEGACY_SITE_ABBREVIATIONS.items():
            if k not in cache:
                cache[k] = v
                changed = True
        return changed
    _site_aliases_store.mutate(_apply)


_seed_site_aliases()

# Lazily-built lookup over known_sites.json: normalize(name) AND
# normalize(network) → canonical spelling. Invalidated whenever the known-sites
# store changes (_add_known_site / bulk add) so new matches teach immediately.
_site_index_cache: dict = {"index": None}


def _invalidate_site_index() -> None:
    _site_index_cache["index"] = None


def _site_lookup(name: str) -> Optional[str]:
    """Resolve a filename site token to a canonical site name (or None).

    Learned aliases win over the known-sites index — a user correction is
    stronger evidence than a spelling merely seen in past matches.
    """
    if not name:
        return None
    key = _perf_normalize(str(name))
    if not key:
        return None
    learned = _site_aliases_store.get_key(key)
    if learned:
        return learned
    idx = _site_index_cache["index"]
    if idx is None:
        idx = {}
        for s in _load_known_sites():
            n = str(s.get("name") or "")
            net = str(s.get("network") or "")
            if n:
                idx.setdefault(_perf_normalize(n), n)
            if net:
                idx.setdefault(_perf_normalize(net), net)
        _site_index_cache["index"] = idx
    return idx.get(key)


def _site_alias_learn(alias: str, canonical: str) -> None:
    """Persist one site alias→canonical mapping (validated, idempotent)."""
    key = _perf_normalize(str(alias or ""))
    canonical = str(canonical or "").strip()
    if (not key or not canonical or len(key) > 120 or len(canonical) > 200
            or key == _perf_normalize(canonical)):
        return

    def _apply(cache: dict) -> bool:
        if cache.get(key) == canonical:
            return False
        cache[key] = canonical
        return True

    _site_aliases_store.mutate(_apply)


# Performer portrait URLs (F7): {normalized name: image_url}, filled
# OPPORTUNISTICALLY from the search_performer results the alias learner
# already paid for — never a dedicated API call. Consumed by write_nfo as
# <actor><thumb>. Same DATA_DIR JSON-store pattern as the alias table.
_PERFORMER_IMAGES_FILE = DATA_DIR / "performer_images.json"
_performer_images_store = _JsonStore(_PERFORMER_IMAGES_FILE, dict)
_PERFORMER_IMAGES_MAX = 10000   # sanity cap — ~1 URL per performer ever seen


def _performer_image_lookup(name: str) -> Optional[str]:
    """Resolve a performer name to a stored portrait URL (or None)."""
    if not name:
        return None
    return _performer_images_store.get_key(_perf_normalize(str(name)))


def _performer_image_learn(name: str, image_url) -> None:
    """Persist one performer→portrait mapping (validated, capped, idempotent)."""
    key = _perf_normalize(str(name or ""))
    url = str(image_url or "").strip()
    if (not key or not url or len(url) > 500
            or not url.lower().startswith(("http://", "https://"))):
        return

    def _apply(cache: dict) -> bool:
        if cache.get(key) == url:
            return False
        if key not in cache and len(cache) >= _PERFORMER_IMAGES_MAX:
            return False
        cache[key] = url
        return True

    _performer_images_store.mutate(_apply)

# Per-run cap on TPDB performer lookups while learning — a 300-file confirmed
# batch must not fan out into hundreds of API calls.
_ALIAS_LEARN_MAX_LOOKUPS = 5


def _alias_lookup(name: str) -> Optional[str]:
    """Resolve a (filename) performer name to its learned canonical name."""
    if not name:
        return None
    return _performer_aliases_store.get_key(_perf_normalize(str(name)))


def _alias_learn(alias: str, canonical: str) -> None:
    """Persist one alias→canonical mapping (no-op for empty/identity pairs)."""
    key = _perf_normalize(str(alias or ""))
    canonical = str(canonical or "").strip()
    if not key or not canonical or key == _perf_normalize(canonical):
        return

    def _apply(cache: dict) -> bool:
        if cache.get(key) == canonical:
            return False
        cache[key] = canonical
        return True

    _performer_aliases_store.mutate(_apply)


def _maybe_submit_fingerprints(scene: dict, file_data: dict) -> int:
    """F5: schedule an OPT-IN fingerprint contribution for a user-confirmed
    StashDB match. Returns 1 when a background submission was scheduled, else 0
    (callers enforce a per-request cap with the sum).

    Hard gates, all must hold:
      • the "contribute_fingerprints" setting is EXPLICITLY True (default off —
        with the toggle off this function is a guaranteed no-op, zero traffic);
      • the scene is StashDB-sourced with a real UUID id;
      • the file has a validated 16-hex oshash;
      • a positive duration exists (the stash-box schema requires it).
    Only content hashes + duration ever leave the machine — never names/paths.
    """
    if _load_settings().get("contribute_fingerprints") is not True:
        return 0
    if stashdb is None or not isinstance(scene, dict):
        return 0
    if (scene.get("source") or "") != "stashdb":
        return 0
    scene_id = str(scene.get("id") or "")
    if not _UUID_RE.fullmatch(scene_id):
        return 0
    oshash = file_data.get("oshash")
    if not (isinstance(oshash, str) and re.fullmatch(r"[0-9a-f]{16}", oshash)):
        return 0
    duration = file_data.get("duration_seconds") or scene.get("duration")
    try:
        if not duration or float(duration) <= 0:
            return 0
    except (TypeError, ValueError):
        return 0
    phash = file_data.get("phash")
    if not (isinstance(phash, str) and re.fullmatch(r"[0-9a-f]{16}", phash)):
        phash = None
    asyncio.create_task(stashdb.submit_fingerprint(
        scene_id, oshash=oshash, phash=phash, duration=duration))
    return 1


def _seed_credit_aliases(scene: dict) -> None:
    """F6: learn alias→canonical pairs from StashDB per-scene credits — free.

    A credit's ``as`` is the alias the performer was credited under in THIS
    scene, so the pair needs no TPDB lookup. Only runs on confirmed matches
    (rename / confirm endpoints) — never on raw auto-matches — so a wrong
    auto-match can't poison the table. Values are length- and count-capped
    because /api/confirm-match scenes cross the API boundary.
    """
    credits = scene.get("performer_credits")
    if not isinstance(credits, list):
        return
    for credit in credits[:30]:
        if not isinstance(credit, dict):
            continue
        alias = credit.get("as")
        name = credit.get("name")
        if (isinstance(alias, str) and isinstance(name, str)
                and 0 < len(alias) <= 120 and 0 < len(name) <= 120):
            _alias_learn(alias, name)   # idempotent; identity pairs no-op


async def _learn_aliases(pairs: list) -> None:
    """Background task: learn alias→canonical mappings from confirmed matches.

    For each (filename_performers, api_performers) pair of a user-confirmed
    rename, take the filename names that scored <0.5 against EVERY API name and
    ask TPDB once whether that name is a known alias of one of the scene's
    performers — a strict bidirectional check (searched name in result aliases
    AND result canonical among the scene's performers) so junk can't be
    learned. Capped, best-effort, and silently disabled without a TPDB key.
    """
    if tpdb is None or not pairs:
        return
    lookups = 0
    try:
        for file_performers, api_performers in pairs:
            if lookups >= _ALIAS_LEARN_MAX_LOOKUPS:
                break
            if not file_performers or not api_performers:
                continue
            api_norm = {_perf_normalize(str(a)) for a in api_performers}
            for f_perf in file_performers:
                if lookups >= _ALIAS_LEARN_MAX_LOOKUPS:
                    break
                f_norm = _perf_normalize(str(f_perf))
                if not f_norm or _alias_lookup(f_perf):
                    continue  # empty or already learned
                best = max((_performer_pair_score(f_norm, a) for a in api_norm),
                           default=0.0)
                # Skip only CONFIDENT fuzzy matches. An exact-0.5 partial hit
                # (single shared name token, PARTIAL_NAME_SCORE) is precisely
                # the ambiguity a learned alias resolves, so it stays eligible.
                if best > _PARTIAL_NAME_SCORE:
                    continue
                lookups += 1
                results = await tpdb.search_performer(str(f_perf))
                for perf in results or []:
                    # F7: bank every portrait this already-paid-for lookup
                    # returned — write_nfo turns them into <actor><thumb>.
                    _performer_image_learn(perf.name, getattr(perf, "image_url", None))
                for perf in results or []:
                    name_norm = _perf_normalize(perf.name or "")
                    alias_hit = any(_perf_normalize(al) == f_norm
                                    for al in (perf.aliases or []))
                    if alias_hit and name_norm in api_norm:
                        _alias_learn(str(f_perf), perf.name)
                        print(f"INFO: learned performer alias: {f_perf!r} -> {perf.name!r}")
                        break
    except Exception as e:  # learning must never break a rename
        print(f"WARNING: alias learning failed: {e}")


class ConfirmMatchRequest(BaseModel):
    """Request body for /api/confirm-match (F3 — user picked an alternative)."""
    oshash: str
    scene: dict

    @field_validator("oshash")
    @classmethod
    def validate_oshash(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{16}", v):
            raise ValueError("oshash must be 16 hex characters")
        return v


@app.post("/api/confirm-match")
async def confirm_match(req: ConfirmMatchRequest):
    """Persist a user-picked scene as the confirmed match for a content hash (F3).

    Same trust level and store as the manual-save confirm: the entry is keyed by
    the file's oshash, flagged user_confirmed (never evicted, trusted by the
    next scan/match), and carries the scene's own provider source.
    """
    if not str(req.scene.get("title") or "").strip():
        raise HTTPException(status_code=400, detail="Scene must have a title")
    source = str(req.scene.get("source") or "manual").strip() or "manual"
    _match_cache_confirm(req.oshash, req.scene, source)
    # F6: a user-picked scene is ground truth — seed its credit aliases too
    # (capped + validated inside; scene dicts here cross the API boundary).
    _seed_credit_aliases(req.scene)
    # F5 (OPT-IN, default off): a picked StashDB scene is a confirm — the
    # oshash comes from the validated request; duration from the scene.
    _maybe_submit_fingerprints(req.scene, {"oshash": req.oshash})
    return {"success": True}


class AddKnownSiteRequest(BaseModel):
    name: str
    network: str = ""

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Site name cannot be empty")
        if len(v) > 200:
            raise ValueError("Site name too long")
        return v

    @field_validator("network")
    @classmethod
    def _validate_network(cls, v: str) -> str:
        return v.strip()[:200]


@app.post("/api/known-sites")
async def add_known_site_manual(req: AddKnownSiteRequest):
    """Manually register a site name (called when user picks a site in manual edit)."""
    _add_known_site(req.name, req.network)
    return {"ok": True}


class ThumbnailRequest(BaseModel):
    file_path: str

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        p = Path(v).resolve()
        if not _is_allowed_path(p):
            raise ValueError("Path is not in an allowed media directory")
        if p.suffix.lower() not in VIDEO_EXTENSIONS:
            raise ValueError("Not a video file")
        return str(p)


def _thumbnail_dir_for(file_path: Path) -> Path:
    """Collision-free thumbnail directory for one video (roadmap-2 F1).

    Keyed by stem PLUS an 8-char sha1 of the full path — two `scene.mp4` files
    in different folders previously shared (and overwrote) one directory. The
    hash is a path-bucketing key, not a security primitive. Shared by the
    extract endpoint and the manual-save selected-poster flow so the two can
    never diverge; the serve route needs no change (the client echoes whatever
    directory name the server handed out in `thumbnail_url`).
    """
    suffix = hashlib.sha1(str(file_path).encode("utf-8")).hexdigest()[:8]
    return DATA_DIR / "thumbnails" / f"{file_path.stem}-{suffix}"


@app.post("/api/extract-thumbnails")
async def extract_thumbnails(request: ThumbnailRequest):
    """
    Extract multiple thumbnails from a video file using ffmpeg.
    Returns base64-encoded images for preview.
    """
    file_path = Path(request.file_path)

    # Defense-in-depth (roadmap-2 F1): ThumbnailRequest.validate_file_path
    # already rejects non-allowed paths at the model layer (422), so this is
    # normally unreachable — it exists so a future model refactor can't silently
    # drop the allowlist for this endpoint.
    if not _is_allowed_path(file_path):
        raise HTTPException(status_code=403, detail="Path not in an allowed media directory")

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found")
    
    try:
        # Get video duration first
        probe_cmd = [
            ffprobe_path(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        duration = float(result.stdout.strip())
        
        # Generate 6 thumbnails at different timestamps
        thumbnails = []
        thumbnail_dir = _thumbnail_dir_for(file_path)
        thumbnail_dir.mkdir(parents=True, exist_ok=True)
        
        # Extract thumbnails at 10%, 25%, 40%, 55%, 70%, 85% of duration
        percentages = [0.10, 0.25, 0.40, 0.55, 0.70, 0.85]
        
        for idx, pct in enumerate(percentages):
            timestamp = duration * pct
            thumb_path = thumbnail_dir / f"thumb_{idx}.jpg"
            
            # Extract frame with ffmpeg
            ffmpeg_cmd = [
                ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
                "-ss", str(timestamp),
                "-i", str(file_path),
                "-vframes", "1",
                "-q:v", "2",
                "-vf", "scale=320:-1",
                str(thumb_path)
            ]
            
            subprocess.run(ffmpeg_cmd, capture_output=True, timeout=30, check=True)
            
            # Read and encode to base64
            if thumb_path.exists():
                with open(thumb_path, "rb") as f:
                    img_data = base64.b64encode(f.read()).decode('utf-8')
                    thumbnails.append({
                        "index": idx,
                        "timestamp": round(timestamp, 2),
                        "data": f"data:image/jpeg;base64,{img_data}",
                        "path": str(thumb_path)
                    })
        
        return {
            "success": True,
            "file_path": str(file_path),
            "duration": round(duration, 2),
            "thumbnails": thumbnails
        }
        
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="Thumbnail extraction timed out")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"FFmpeg error: {e.stderr}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to extract thumbnails: {str(e)}")


# ─── Write-NFO endpoint ────────────────────────────────────────────────

class WriteNfoRequest(BaseModel):
    file_path: str
    scene_data: dict
    # Scan-entry fields for the file (duration_seconds/quality/video_format) so
    # a per-row "Write NFO" carries the same enrichment as a Phase-2 NFO (F5).
    # Optional — older clients simply produce an NFO without stream details.
    file_data: dict = {}

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v: str) -> str:
        p = Path(v).resolve()
        if not _is_allowed_path(p):
            raise ValueError("Path not in an allowed media directory")
        return str(p)


@app.post("/api/write-nfo")
async def write_nfo_endpoint(req: WriteNfoRequest):
    """
    Write (or overwrite) a Jellyfin/Plex-compatible NFO sidecar for a video
    that has already been matched but does not need renaming.

    The NFO is placed next to the video file with the same stem:
      /mnt/NAS/Folder/MyVideo.mp4  →  /mnt/NAS/Folder/MyVideo.nfo
    """
    file_path = Path(req.file_path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {file_path}")

    meta = {
        "title":        req.scene_data.get("title", ""),
        "site":         req.scene_data.get("site", ""),
        "performers":   req.scene_data.get("performers", []),
        "release_date": req.scene_data.get("release_date", ""),
        "tags":         req.scene_data.get("tags", []),
        "description":  req.scene_data.get("description", ""),
        # Provider id/source → <uniqueid> (F4). The scene dict carries "source".
        "id":           req.scene_data.get("id", ""),
        "source":       req.scene_data.get("source") or "tpdb",
        # NFO enrichment (F5) — mirrors the Phase-2 meta build in rename_files.
        "duration_seconds": req.file_data.get("duration_seconds") or req.scene_data.get("duration"),
        "network":      req.scene_data.get("network", ""),
        "quality":      req.file_data.get("quality", ""),
        "video_format": req.file_data.get("video_format", ""),
        # NFO round three (F7): provider page link + fanart backdrop.
        "url":          req.scene_data.get("url", ""),
        "fanart_url":   req.scene_data.get("fanart_url", ""),
    }
    try:
        write_nfo(file_path, meta)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"NFO write failed: {e}")

    nfo_path = file_path.with_suffix(".nfo")
    return {"success": True, "nfo_path": str(nfo_path)}


# ─── Metadata embedding helper ──────────────────────────────────────────

# Detected quality label → video height for <streamdetails> (F5). Keys are
# lowercased detector tokens (QUALITY_PATTERN: 720p|1080p|2160p|4K|UHD).
# Unknown/unparseable labels are simply omitted from the NFO.
_NFO_QUALITY_HEIGHTS: dict[str, int] = {
    "720p": 720, "1080p": 1080, "2160p": 2160, "4k": 2160, "uhd": 2160,
}


def write_nfo(file_path: Path, metadata: dict) -> None:
    """
    Write a Jellyfin-compatible <video>.nfo sidecar next to the video file.
    Always overwrites, so there is exactly one NFO per video — never more.
    """
    import xml.etree.ElementTree as ET

    title        = metadata.get("title", "")
    site         = metadata.get("site", "")
    performers   = metadata.get("performers", [])
    release_date = metadata.get("release_date", "")
    tags         = metadata.get("tags", [])
    description  = metadata.get("description", "")
    # Provider linkage (F4): a scene id + its source ("tpdb"/"stashdb") become a
    # <uniqueid> so Jellyfin/Kodi can link back to the metadata provider. Skipped
    # for manual entries (id == "manual") and when no id is present.
    scene_id     = str(metadata.get("id") or "").strip()
    source       = (str(metadata.get("source") or "").strip() or "tpdb")
    # Relative filename of a poster image sitting next to the video (e.g.
    # "MyScene-poster.jpg"). When present, referenced from the NFO so
    # Jellyfin/Plex/Kodi display the chosen still. Empty → no poster refs.
    poster_path  = (metadata.get("poster_path") or "").strip()
    year         = release_date[:4] if release_date else ""
    # NFO enrichment (F5) — data AMM already had in memory at write time:
    # probed/API duration → <runtime> (whole minutes), parsed network → second
    # <studio> (Jellyfin merges multiple studio elements), detector quality/
    # video_format → <fileinfo><streamdetails><video>. All optional: files
    # without them omit the elements entirely (never a zero/empty tag).
    network      = (metadata.get("network") or "").strip()
    runtime_min  = 0
    try:
        runtime_min = int(float(metadata.get("duration_seconds") or 0) // 60)
    except (TypeError, ValueError):
        pass
    # Codec token normalised to player conventions: lowercase, dots stripped
    # ("H.264" → "h264"; "x265" stays "x265").
    codec        = (metadata.get("video_format") or "").strip().lower().replace(".", "")
    height       = _NFO_QUALITY_HEIGHTS.get((metadata.get("quality") or "").strip().lower())
    # NFO round three (F7): provider page link + fanart backdrop. Both are
    # plain REFERENCES — AMM never downloads fanart itself, players fetch it
    # (so AMM_FETCH_POSTERS=0 zero-egress deployments stay zero-egress).
    scene_url    = (metadata.get("url") or "").strip()
    fanart_url   = (metadata.get("fanart_url") or "").strip()

    root = ET.Element("movie")
    ET.SubElement(root, "title").text         = title
    ET.SubElement(root, "originaltitle").text  = title
    ET.SubElement(root, "sorttitle").text      = title
    if year:
        ET.SubElement(root, "year").text       = year
    if release_date:
        ET.SubElement(root, "releasedate").text = release_date
        ET.SubElement(root, "premiered").text   = release_date
    if site:
        ET.SubElement(root, "studio").text     = site
    # Network as a second <studio> — case-insensitive compare so "VIXEN" vs
    # "Vixen" never produces a duplicate entry.
    if network and network.lower() != site.lower():
        ET.SubElement(root, "studio").text     = network
    if runtime_min > 0:
        ET.SubElement(root, "runtime").text    = str(runtime_min)
    if description:
        ET.SubElement(root, "plot").text       = description
        ET.SubElement(root, "outline").text    = description
    # Provider id → <uniqueid type="tpdb|stashdb" default="true">. Skip the
    # sentinel "manual" id (not a real provider reference).
    if scene_id and scene_id.lower() != "manual":
        ET.SubElement(root, "uniqueid",
                      {"type": source, "default": "true"}).text = scene_id
    ET.SubElement(root, "genre").text          = "Adult"
    # One <genre> per scene tag (in addition to the base "Adult"), so players can
    # browse by genre. Tags still also appear as <tag> below.
    for tag in tags:
        ET.SubElement(root, "genre").text      = tag
    # Poster references. <thumb> is Kodi's classic form; <art><poster> is the
    # Jellyfin/Kodi-v17+ form — writing both maximises player compatibility. The
    # value is a plain relative filename, resolved by the player next to the NFO.
    if poster_path:
        ET.SubElement(root, "thumb", {"aspect": "poster"}).text = poster_path
        art = ET.SubElement(root, "art")
        ET.SubElement(art, "poster").text = poster_path
    # Provider page link (F7) — Kodi/Jellyfin's standard <url> element.
    if scene_url:
        ET.SubElement(root, "url").text = scene_url
    # Fanart backdrop (F7) — remote-URL form; the player fetches it, AMM never
    # does. Omitted entirely when the scene has no distinct backdrop image.
    if fanart_url:
        fanart_el = ET.SubElement(root, "fanart")
        ET.SubElement(fanart_el, "thumb").text = fanart_url
    for tag in tags:
        ET.SubElement(root, "tag").text        = tag
    for performer in performers:
        actor_el = ET.SubElement(root, "actor")
        ET.SubElement(actor_el, "name").text   = performer
        # Actor portrait (F7) — only when the alias learner already banked one;
        # name-only actors stay exactly as before.
        actor_thumb = _performer_image_lookup(performer)
        if actor_thumb:
            ET.SubElement(actor_el, "thumb").text = actor_thumb

    # Stream details (F5) — what Jellyfin/Kodi use for resolution/codec badges
    # and filtering. Only written when at least one field is known.
    if codec or height:
        fileinfo = ET.SubElement(root, "fileinfo")
        stream   = ET.SubElement(fileinfo, "streamdetails")
        video    = ET.SubElement(stream, "video")
        if codec:
            ET.SubElement(video, "codec").text  = codec
        if height:
            ET.SubElement(video, "height").text = str(height)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    nfo_path = file_path.with_suffix(".nfo")
    with open(nfo_path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="unicode", xml_declaration=False)


# Containers FFmpeg cannot write to at all (read-only decoders).
# For these we skip FFmpeg and rely entirely on the NFO sidecar.
_FFMPEG_NO_WRITE_EXTS: frozenset[str] = frozenset({
    ".rmvb", ".rm", ".ra", ".rv",   # RealMedia
    ".nsv",                            # Nullsoft Streaming Video
    ".roq",                            # id Software RoQ
    ".amv",                            # AMV (proprietary MJPEG variant)
    ".ifo",                            # DVD IFO (not a stream)
    ".dif",                            # DV interchange (raw, no muxer)
    ".hevc", ".h264", ".h265",         # Naked elementary streams
    ".fli", ".flc",                    # FLIC animation
})

# Map file extensions to the output suffix FFmpeg should produce.
# Keeps the same container where possible; deviates only when the default
# muxer rejects common codecs (e.g. iPod muxer for .m4v rejects HEVC).
_FFMPEG_OUT_SUFFIX: dict[str, str] = {
    ".m4v":  ".mp4",   # iPod muxer → mp4 muxer (supports all codecs)
    ".m4b":  ".mp4",
    ".m4p":  ".mp4",
    ".ogm":  ".ogv",   # ogm is read-only in FFmpeg; use ogv muxer
    ".xvid": ".avi",
    ".divx": ".avi",
    ".trp":  ".ts",
    ".tp":   ".ts",
    ".mpe":  ".mpg",
    ".m1v":  ".mpg",
    ".m2v":  ".mpg",
    ".dvr-ms": ".wmv",
    ".wtv":  ".wmv",
    ".3gpp": ".3gp",
    ".3gpp2": ".3g2",
    ".f4p":  ".f4v",
    ".mk3d": ".mkv",
    ".qt":   ".mov",
}

async def embed_metadata(file_path: Path, metadata: dict) -> tuple[bool, str]:
    """
    Embed metadata into a video file using FFmpeg -codec copy (fast, no re-encode).
    Uses a temp file to avoid corrupting the original on failure.

    Formats that FFmpeg cannot write are skipped silently — the caller always
    writes a companion NFO sidecar which carries the metadata for those files.

    Mapping:
        title       → title tag
        performers  → artist tag  (comma-separated)
        site        → album tag
        release_date→ date tag
        tags        → comment tag (comma-separated)
        description → description + synopsis tags
    """
    ext = file_path.suffix.lower()

    # Formats FFmpeg cannot write: skip embedding, NFO sidecar will carry data.
    if ext in _FFMPEG_NO_WRITE_EXTS:
        return True, ""

    # Resolve the output suffix (same container where safe; remapped otherwise).
    out_suffix = _FFMPEG_OUT_SUFFIX.get(ext, ext)

    # Phase 1 — FFmpeg writes to the local staging dir (_EMBED_STAGING_DIR),
    # which lives on the Docker named volume (/data), NOT on the NAS/FUSE mount.
    # Benefits:
    #   • FFmpeg I/O is local-disk speed; NAS bandwidth is only used for the
    #     final verified copy-back, not for the entire re-mux pass.
    #   • No work files ever appear in the user's media library.
    #   • The original file is not touched until the final atomic replace.
    #   • A crash leaves stale files in /data/embed-tmp, purged on next start.
    tmp_path: Optional[Path] = None
    tmp_fd, tmp_str = tempfile.mkstemp(suffix=out_suffix, dir=_EMBED_STAGING_DIR)
    os.close(tmp_fd)
    tmp_path = Path(tmp_str)

    try:
        cmd = [
            ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(file_path),
            # Map only video, audio, and subtitle streams (? = skip if absent).
            # Deliberately excludes data/timecode streams (codec=none) that
            # many MP4 muxers reject, causing "Invalid argument" header errors.
            "-map", "0:v?",
            "-map", "0:a?",
            "-map", "0:s?",
            "-codec", "copy",
            "-map_metadata", "0",          # keep existing metadata as base
        ]

        # Shared field→tag mapping (also used by the mkvpropedit / AtomicParsley
        # writers) so embedded metadata is identical whichever strategy runs.
        cmd.extend(ffmpeg_metadata_args(metadata))

        cmd.append(str(tmp_path))

        # Dynamic timeout: 300 s base + 180 s per GB, capped at 3600 s (1 hour).
        # -codec copy is I/O-bound; large files on slow NAS mounts need generous
        # headroom.  The previous formula under-estimated for files > 2.5 GB.
        try:
            file_size_bytes = file_path.stat().st_size
            timeout = min(3600, 300 + int(file_size_bytes / (1024 ** 3) * 180))
        except OSError:
            timeout = 600

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            tmp_path.unlink(missing_ok=True)
            return False, f"FFmpeg timed out after {timeout}s during metadata embedding"

        if proc.returncode != 0:
            stderr_text = stderr_data.decode(errors="replace").strip()
            # If a subtitle codec is also unsupported in the container, retry
            # with only video + audio streams (most permissive fallback).
            if "not currently supported in container" in stderr_text or \
               "Could not find tag for codec" in stderr_text:
                tmp_path.unlink(missing_ok=True)
                # Rebuild with video+audio only
                cmd_va = [arg if arg not in ("0:s?",) else None for arg in cmd]
                cmd_va = [a for a in cmd_va if a is not None]
                # Replace the three -map entries with just v+a
                try:
                    map_start = cmd_va.index("-map")
                except ValueError:
                    return False, stderr_text
                # Remove old map args and insert fresh ones
                while "-map" in cmd_va:
                    i = cmd_va.index("-map")
                    cmd_va.pop(i)   # -map
                    cmd_va.pop(i)   # its value
                ins = cmd_va.index("-codec")
                cmd_va[ins:ins] = ["-map", "0:v?", "-map", "0:a?"]
                tmp_fd2, tmp_str2 = tempfile.mkstemp(suffix=out_suffix, dir=_EMBED_STAGING_DIR)
                os.close(tmp_fd2)
                tmp_path = Path(tmp_str2)
                cmd_va[-1] = str(tmp_path)
                proc2 = await asyncio.create_subprocess_exec(
                    *cmd_va,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _, stderr_data2 = await asyncio.wait_for(proc2.communicate(), timeout=timeout)
                except asyncio.TimeoutError:
                    proc2.kill()
                    await proc2.wait()
                    tmp_path.unlink(missing_ok=True)
                    return False, f"FFmpeg timed out after {timeout}s during metadata embedding"
                if proc2.returncode != 0:
                    tmp_path.unlink(missing_ok=True)
                    return False, stderr_data2.decode(errors="replace").strip()
            else:
                tmp_path.unlink(missing_ok=True)
                return False, stderr_text

        # ── Three-phase commit ────────────────────────────────────────────────
        # Guard: empty output = silent FFmpeg failure (e.g. staging disk full).
        try:
            if tmp_path.stat().st_size == 0:
                tmp_path.unlink(missing_ok=True)
                return False, "FFmpeg produced an empty output file"
        except OSError:
            pass

        import shutil as _shutil

        # Phase 2: copy local staging temp → NAS staging temp.
        # The .amm_ prefix means the scan filter already ignores it.
        # Same-dir placement ensures Phase 3 is an intra-filesystem rename.
        nas_tmp: Optional[Path] = None
        try:
            try:
                nas_fd, nas_str = tempfile.mkstemp(
                    prefix=".amm_", suffix=out_suffix, dir=file_path.parent
                )
                os.close(nas_fd)
                nas_tmp = Path(nas_str)
                try:
                    _shutil.copy2(str(tmp_path), str(nas_tmp))
                except OSError:
                    try:
                        _shutil.copy(str(tmp_path), str(nas_tmp))
                    except OSError:
                        _shutil.copyfile(str(tmp_path), str(nas_tmp))

                # Phase 3: atomic replace — original only changes at this instant.
                os.replace(str(nas_tmp), str(file_path))
                nas_tmp = None  # consumed; nothing to clean up
                return True, ""

            except OSError as _stage_err:
                # NAS staging not possible (permissions, read-only share, …).
                # Last-resort direct overwrite: original is at risk only during
                # the copy window, but we must not silently discard the result.
                if nas_tmp and nas_tmp.exists():
                    nas_tmp.unlink(missing_ok=True)
                try:
                    _shutil.copy2(str(tmp_path), str(file_path))
                except OSError:
                    try:
                        _shutil.copy(str(tmp_path), str(file_path))
                    except OSError:
                        _shutil.copyfile(str(tmp_path), str(file_path))
                return True, ""
        finally:
            tmp_path.unlink(missing_ok=True)

    except Exception as e:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        return False, str(e)


async def embed_metadata_mkv(file_path: Path, metadata: dict) -> tuple[bool, str]:
    """
    Embed metadata into a Matroska file IN PLACE using mkvpropedit.

    Only the segment title and global tags are rewritten — the media data is
    never re-muxed, so this is near-instant even on multi-GB files and uses
    negligible NAS bandwidth (the key win over the FFmpeg remux).

    Returns (True, "") on success (including the no-op case), or (False, err);
    the caller falls back to the FFmpeg remux on failure.
    """
    binary = _mkvpropedit_path()
    if not binary:
        return False, "mkvpropedit not available"

    title = (metadata.get("title") or "").strip()
    tags_xml = build_mkv_tags_xml(metadata)

    # Nothing to write → success no-op.
    if not title and tags_xml is None:
        return True, ""

    cmd = [binary, str(file_path)]
    if title:
        # Passed as a single argv element (no shell) — value may contain '=' or
        # spaces safely; mkvpropedit splits on the first '='.
        cmd += ["--edit", "info", "--set", f"title={title}"]

    tags_tmp: Optional[Path] = None
    try:
        if tags_xml is not None:
            fd, tmp_str = tempfile.mkstemp(suffix=".xml", dir=_EMBED_STAGING_DIR)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
                fh.write('<!DOCTYPE Tags SYSTEM "matroskatags.dtd">\n')
                fh.write(tags_xml)
            tags_tmp = Path(tmp_str)
            cmd += ["--tags", f"global:{tags_tmp}"]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, "mkvpropedit timed out"

        # mkvtoolnix exit codes: 0 = ok, 1 = warning(s) but changes were applied,
        # 2 = error/aborted.  Treat 0 and 1 as success so we don't trigger a
        # needless remux when the edit actually happened.
        if proc.returncode not in (0, 1):
            return False, stderr_data.decode(errors="replace").strip() or "mkvpropedit failed"
        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        if tags_tmp is not None:
            tags_tmp.unlink(missing_ok=True)


async def embed_metadata_mp4(file_path: Path, metadata: dict) -> tuple[bool, str]:
    """
    Tag an MP4/M4V/MOV file IN PLACE using AtomicParsley (``--overWrite``).

    Avoids the FFmpeg stream-remux quirks (data/subtitle stream rejections) and
    keeps the media untouched. Field mapping mirrors the FFmpeg/mkv writers:
        title → --title, performers → --artist, site → --album,
        release_date → --year, tags → --comment,
        description → --description (--longdesc carries the full text when it
        exceeds AtomicParsley's 255-char description limit).

    Returns (True, "") on success (including the no-op case), or (False, err);
    the caller falls back to the FFmpeg remux on failure.
    """
    binary = _atomicparsley_path()
    if not binary:
        return False, "AtomicParsley not available"

    cmd = [binary, str(file_path)]
    desc = (metadata.get("description") or "").strip()
    mapping = [
        ("--title",   (metadata.get("title") or "").strip()),
        ("--artist",  ", ".join(metadata.get("performers", []) or [])),
        ("--album",   (metadata.get("site") or "")),
        ("--year",    (metadata.get("release_date") or "")),
        ("--comment", ", ".join(metadata.get("tags", []) or [])),
        ("--description", desc[:255]),
        ("--longdesc",    desc if len(desc) > 255 else ""),
    ]
    for flag, value in mapping:
        if value:
            cmd += [flag, value]

    # Nothing to write → success no-op (don't rewrite the file for nothing).
    if len(cmd) == 2:
        return True, ""

    cmd.append("--overWrite")

    # AtomicParsley may rewrite the moov atom; size-scaled timeout like the remux.
    try:
        timeout = min(3600, 300 + int(file_path.stat().st_size / (1024 ** 3) * 180))
    except OSError:
        timeout = 600

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"AtomicParsley timed out after {timeout}s"
        if proc.returncode != 0:
            return False, stderr_data.decode(errors="replace").strip() or "AtomicParsley failed"
        return True, ""
    except Exception as e:
        return False, str(e)


# Strategy registry (review item R4): name → async executor. The planner
# (embedder.plan_embed) returns an ordered list of these names; _embed_for_mode
# tries each until one succeeds, with "remux" always last as the universal
# fallback. Adding a new strategy = one executor + one registry entry + one
# branch in plan_embed.
_EMBED_EXECUTORS = {
    "mkvpropedit":   lambda fp, md: embed_metadata_mkv(fp, md),
    "atomicparsley": lambda fp, md: embed_metadata_mp4(fp, md),
    "remux":         lambda fp, md: embed_metadata(fp, md),
}


async def _embed_for_mode(file_path: Path, metadata: dict, embed_mode: str) -> tuple[bool, str]:
    """
    Write embedded metadata according to embed_mode (never called for nfo_only).

    Delegates the per-container decision to the pure planner
    (embedder.plan_embed), then runs the resulting strategy chain in order until
    one succeeds. "smart" prefers a fast in-place edit (mkvpropedit for Matroska,
    AtomicParsley for MP4) and falls back to the FFmpeg remux; "embed" is remux
    only. The end state always matches "embed" because "remux" is the last step.
    """
    plan = plan_embed(
        file_path.suffix.lower(),
        embed_mode,
        has_mkvpropedit=bool(_mkvpropedit_path()),
        has_atomicparsley=bool(_atomicparsley_path()),
    )
    if not plan:
        return True, ""  # nothing to do (e.g. nfo_only) — safe no-op

    errors: list[str] = []
    for strategy in plan:
        ok, err = await _EMBED_EXECUTORS[strategy](file_path, metadata)
        if ok:
            return True, ""
        errors.append(f"{strategy}: {err}")
    return False, "; ".join(errors)


@app.post("/api/save-manual-metadata")
async def save_manual_metadata(req: ManualMetadataRequest, background_tasks: BackgroundTasks):
    """
    Persist manually entered metadata.

    Fast path (returns immediately): write the Jellyfin/Plex-compatible NFO
    sidecar and select the preferred thumbnail.  Players read the sidecar, so the
    edit is durable the moment this returns — the request is no longer gated on a
    multi-GB container rewrite (review item X3).

    Heavy path (background): for embed_mode "embed"/"smart" the in-container
    FFmpeg remux is dispatched as a BackgroundTask and tracked in _embed_jobs,
    exactly like Phase 2 of /api/rename.  The client polls
    /api/embed-status/{job_id}.  "nfo_only" skips embedding entirely (no job).
    """
    file_path = Path(req.file_path)

    # Security: confine writes to the configured media roots — native mode
    # (AMM_NATIVE=1) allows any absolute path, Docker restricts to the mounted
    # allowlist.  Mirrors the guard already enforced by /api/preview-paths and
    # /api/browse so this write endpoint isn't a softer target.
    if not _is_allowed_path(file_path):
        raise HTTPException(status_code=403, detail="Path not in an allowed media directory")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    metadata = {
        "title":        req.title,
        "site":         req.site or "",
        "performers":   req.performers,
        "release_date": req.release_date or "",
        "tags":         req.tags,
        # Manually chosen resolution (e.g. "1080p"). Kept so it isn't discarded on
        # save; it rides along in the confirmed-match cache scene below and feeds
        # the {quality} naming variable at a later rename (see formatter fallback).
        "quality":      req.quality or "",
        # Synopsis + provider identity (F7): write_nfo emits <plot> and — when a
        # real provider id is present — <uniqueid type="tpdb|stashdb">; the
        # container embed carries the description too (F4). Hand-typed saves
        # keep id "manual", which write_nfo deliberately skips.
        "description":  req.description or "",
        "id":           req.scene_id or "manual",
        "source":       req.source or "manual",
    }

    # Container-only modes ("embed_only", "remux_only") write tags but NO
    # sidecar (.nfo / -poster.jpg), so the whole sidecar path below is gated.
    write_sidecar = req.embed_mode not in ("embed_only", "remux_only")

    # ── Preferred thumbnail → poster (cheap local copies) ───────────────────
    # Done BEFORE the NFO write so the NFO can reference the poster. The
    # DATA_DIR/…/selected.jpg copy is AMM's own UI preview (always kept); the
    # <video-stem>-poster.jpg beside the video is a media SIDECAR, so it is only
    # written when write_sidecar. Purely local (ffmpeg frame) — no network.
    thumbnail_url = None
    if req.thumbnail_index is not None:
        thumbnail_dir = _thumbnail_dir_for(file_path)
        source_thumb = thumbnail_dir / f"thumb_{req.thumbnail_index}.jpg"
        if source_thumb.exists():
            dest_thumb = thumbnail_dir / "selected.jpg"
            shutil.copy2(source_thumb, dest_thumb)
            thumbnail_url = f"/api/thumbnail/{thumbnail_dir.name}/selected.jpg"
            if write_sidecar:
                try:
                    poster_dest = file_path.with_name(file_path.stem + "-poster.jpg")
                    shutil.copy2(source_thumb, poster_dest)
                    metadata["poster_path"] = poster_dest.name
                except Exception as poster_err:
                    # Never fail the save because the poster copy failed (e.g. a
                    # read-only media mount) — the NFO/metadata still lands.
                    print(f"WARNING: poster copy failed for {file_path}: {poster_err}")

    # ── Fast path: NFO sidecar (tiny local XML) ─────────────────────────────
    # This is the durable, player-visible result and must not be gated on the
    # heavy container embed, so it is written synchronously and up front. Skipped
    # for embed_only (container tags only).
    if write_sidecar:
        try:
            write_nfo(file_path, metadata)
        except Exception as nfo_err:
            raise HTTPException(status_code=500, detail=f"Failed to write metadata sidecar: {nfo_err}")

    # Pre-embed content hash (F6): used for the user-confirm below AND passed to
    # the background embed job so it can re-key the cache entry once the
    # container write changes the file's bytes. Background tasks run after the
    # response, so the confirm always lands under this hash first.
    try:
        pre_oshash = compute_oshash(file_path)
    except Exception:
        pre_oshash = None

    # ── Heavy path: in-container embed dispatched to the background ──────────
    # "nfo_only" relies on the sidecar alone; every other mode writes container
    # tags off the request path so the UI returns instantly.
    embed_job_id = None
    if req.embed_mode != "nfo_only":
        embed_job_id = uuid.uuid4().hex
        _job_create(embed_job_id, 1, kind="manual_embed")
        background_tasks.add_task(
            _run_manual_embed_job, embed_job_id, file_path, metadata,
            req.embed_mode, pre_oshash,
        )

    # A manual save is the strongest "this is the right scene" signal — record it
    # as a user-confirmed match so a future re-scan trusts it (D3). Best-effort;
    # the oshash is recomputed from the (existing, already path-validated) file.
    try:
        confirmed_scene = {
            # Provider identity survives the round trip (F7): a fetched scene
            # keeps its real id/source, so a LATER template rename of this file
            # writes the correct <uniqueid> too (rename meta reads scene.source).
            "id": metadata["id"],
            "source": metadata["source"],
            "title": metadata["title"],
            "site": metadata["site"],
            "performers": metadata["performers"],
            "release_date": metadata["release_date"],
            "tags": metadata["tags"],
            "description": metadata["description"],
            # Persist the user's quality so a later rename can use {quality} even
            # when the filename carries no resolution (formatter falls back to the
            # scene's quality). Absent for API matches, which have no quality.
            "quality": metadata["quality"],
            "manual_entry": True,
            "thumbnail_url": thumbnail_url,
        }
        # Hash computed once above (F6) — the same value the background embed
        # job receives, so its rekey provably targets this entry.
        _match_cache_confirm(pre_oshash, confirmed_scene, metadata["source"])
        # Catalog (R1): "organised" is the NFO-on-disk signal, so only mark it when
        # a sidecar was written. The match-cache confirm above (oshash-keyed) is
        # kept for every mode — it's match memory, independent of the NFO.
        if write_sidecar:
            catalog.mark_organized(
                str(file_path), oshash=pre_oshash, scene_id=metadata["id"],
                source=metadata["source"], confidence=100.0, confirmed=True,
            )
    except Exception:
        pass

    return {
        "success":       True,
        "thumbnail_url":  thumbnail_url,
        "metadata":       metadata,
        "embed_job_id":   embed_job_id,   # null when nfo_only — nothing to poll
    }


@app.get("/api/thumbnail/{file_stem}/{filename}")
async def serve_thumbnail(file_stem: str, filename: str):
    """
    Serve a generated thumbnail image.
    Path parameters are sanitised to plain filenames before use (no directory
    components allowed) to prevent path-traversal attacks.
    """
    # Strip any directory components — only the bare filename is accepted.
    safe_stem = Path(file_stem).name
    safe_name = Path(filename).name

    # Reject empty, "." and ".." components.
    # Path("..").name returns ".." (non-empty), so an explicit check is required
    # to prevent stem/name from being used as a relative traversal component
    # once concatenated (e.g. DATA_DIR/"thumbnails"/".."/name → DATA_DIR/name).
    _INVALID = {"", ".", ".."}
    if safe_stem in _INVALID or safe_name in _INVALID:
        raise HTTPException(status_code=400, detail="Invalid thumbnail path")

    thumbnail_path = (DATA_DIR / "thumbnails" / safe_stem / safe_name).resolve()

    # Defence-in-depth: confirm the resolved path is still inside the thumbnails
    # subdirectory.  The startswith check uses a trailing "/" to prevent the
    # prefix /data/thumbnails from matching /data/thumbnails2 etc.
    thumbnails_dir = (DATA_DIR / "thumbnails").resolve()
    if not str(thumbnail_path).startswith(str(thumbnails_dir) + "/"):
        raise HTTPException(status_code=403, detail="Access denied")

    if not thumbnail_path.exists():
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    return FileResponse(str(thumbnail_path), media_type="image/jpeg")


# ─── Version / update notifier (F17) ───────────────────────────────────

@app.get("/api/version")
async def get_version():
    """Running version plus (cached) newer-release info from GitHub.

    ``update`` is null when: up to date, check disabled (AMM_UPDATE_CHECK=0),
    or GitHub unreachable — failures are always silent. Read-only: downloading
    and installing updates live in the desktop shell (electron/updater.js),
    never behind an HTTP endpoint.
    """
    return {
        "version": APP_VERSION,
        "native": _AMM_NATIVE,
        "update_check": _AMM_UPDATE_CHECK,
        "releases_url": _RELEASES_URL,
        "update": await _get_update_info(),
    }


# ─── Health Check ──────────────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """
    Health check endpoint. ``tools`` (F2) reports whether each external A/V
    tool actually resolves (env override or PATH) — the capability "doctor"
    behind the Settings system-check line.
    """
    return {
        "status": "healthy",
        "version": APP_VERSION,
        "tpdb_configured": tpdb is not None,
        "stashdb_configured": stashdb is not None,
        "tools": _tool_health(),
    }


# Tool availability, cached (health is polled; which() hits the filesystem).
_tool_health_cache: dict = {"at": 0.0, "data": None}


def _tool_health() -> dict:
    now = _time.time()
    if _tool_health_cache["data"] is not None and now - _tool_health_cache["at"] < 60:
        return _tool_health_cache["data"]
    data = {
        # ffmpeg/ffprobe resolve via AMM_FFMPEG/AMM_FFPROBE (bundled) or PATH.
        "ffmpeg":        shutil.which(ffmpeg_path()) is not None,
        "ffprobe":       shutil.which(ffprobe_path()) is not None,
        "mkvpropedit":   _mkvpropedit_path() is not None,
        "atomicparsley": _atomicparsley_path() is not None,
    }
    _tool_health_cache.update(at=now, data=data)
    return data
