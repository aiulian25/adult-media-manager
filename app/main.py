"""
Adult Media Manager - FastAPI backend.
Serves web UI and provides API endpoints for scanning, matching, and renaming adult content.
"""

import os
import re
import json
import asyncio
import subprocess
import tempfile
import base64
from pathlib import Path
from typing import Optional

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
from app.core.matcher import adult_cascade_score, find_best_match, name_similarity
from app.core.formatter import apply_template, build_new_path, TEMPLATES, extract_template_vars
from app.core.renamer import execute_rename, RenameAction, RenameResult
from app.core.history import RenameHistory, HistoryEntry
from app.api.tpdb import TPDBClient
from app.api.stashdb import StashDBClient, compute_oshash, compute_phash_ffmpeg


app = FastAPI(
    title="Adult Media Manager",
    version="1.0.0",
    description="Professional metadata organizer for adult content"
)

# Serve static files
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Initialize history tracking
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
history = RenameHistory(DATA_DIR / "history.json")

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
_embed_jobs: dict[str, dict] = {}
EMBED_JOB_TTL = 600  # 10 minutes

# ── Persistent user settings (API keys saved via the UI) ──────────────────────
# Keys set through environment variables ALWAYS take precedence.
# The settings file is a fallback for users who did not configure their .env /
# docker-compose environment.  Key values are stored in plain text inside the
# already-secured Docker named volume (/data) — the same place history is kept.
# They are NEVER returned to the browser; only an "active / source" status is.
_SETTINGS_FILE: Path = DATA_DIR / "settings.json"
_SETTINGS_KEY_MAX_LEN = 512   # sanity cap — real keys are well under this


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

# Allowed roots for filesystem access — never include "/" (root of the whole filesystem).
# The native Electron build sets AMM_EXTRA_ROOTS (colon-separated) to add user
# home dirs and removable-media mount points on top of the Docker defaults.
_default_roots: set[Path] = {
    Path("/media"),
    Path("/mnt"),
    Path("/data"),
    Path("/downloads"),
    Path("/organized"),
}
_extra_roots: set[Path] = set()
for _r in os.getenv("AMM_EXTRA_ROOTS", "").split(":"):
    _r = _r.strip()
    if _r and _r != "/":           # safety: never allow bare root
        _extra_roots.add(Path(_r))
ALLOWED_ROOTS: frozenset[Path] = frozenset(_default_roots | _extra_roots)


def _is_allowed_path(p: Path) -> bool:
    """Return True only when *p* is inside one of the ALLOWED_ROOTS."""
    rp = p.resolve()
    return any(
        rp == root or str(rp).startswith(str(root) + "/")
        for root in ALLOWED_ROOTS
    )


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
    """Serve the app logo as an inline SVG (no file needed)."""
    from fastapi.responses import Response
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        '<rect width="64" height="64" rx="12" fill="#1a0030"/>'
        '<circle cx="32" cy="32" r="22" fill="none" stroke="#b24bf3" stroke-width="4"/>'
        '<text x="32" y="40" font-size="28" text-anchor="middle" fill="#b24bf3" font-family="sans-serif">A</text>'
        '</svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


# ─── Models ────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    path: str
    recursive: bool = True

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


class RenameRequest(BaseModel):
    operations: list[dict]
    action: str = "test"

    @field_validator("action")
    @classmethod
    def validate_action(cls, v: str) -> str:
        valid = {a.value for a in RenameAction}
        if v not in valid:
            raise ValueError(f"Invalid action: {v}. Must be one of: {valid}")
        return v


class ManualMetadataRequest(BaseModel):
    file_path: str
    title: str
    site: Optional[str] = None
    performers: list[str] = []
    release_date: Optional[str] = None
    tags: list[str] = []
    quality: Optional[str] = None
    thumbnail_index: Optional[int] = None  # Which generated thumbnail to use


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
        return {
            "title":        _text("title"),
            "site":         _text("studio"),
            "performers":   performers,
            "release_date": _text("premiered") or _text("releasedate"),
            "tags":         tags,
            "description":  _text("plot"),
        }
    except Exception:
        return None


# ─── Scan Endpoint ─────────────────────────────────────────────────────

@app.post("/api/scan")
async def scan_directory(req: ScanRequest):
    """
    Scan a directory for adult media files and auto-detect metadata.
    Supports:
    - Single directory path
    - Single file path
    - Comma-separated file paths
    """
    media_exts = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS
    files = []
    
    # Check if path contains comma-separated file paths
    if ',' in req.path:
        # Multiple file paths
        file_paths = [Path(p.strip()) for p in req.path.split(',')]
        paths = [p for p in file_paths if p.exists()]
    else:
        # Single path (directory or file)
        base = Path(req.path)
        if base.is_file():
            paths = [base]
        elif base.is_dir():
            if req.recursive:
                paths = sorted(base.rglob("*"))
            else:
                paths = sorted(base.iterdir())
        else:
            return {"count": 0, "files": [], "error": f"Path not found: {req.path}"}

    for p in paths:
        if not p.is_file():
            continue
        # Skip hidden files: .fuse_hidden* stubs left by FUSE rename-on-open,
        # .DS_Store, .Trash, and any other dot-prefixed filesystem artefact.
        if p.name.startswith('.'):
            continue
        if p.suffix.lower() not in media_exts:
            continue

        det = detect(p)

        # ── NFO sidecar detection ──────────────────────────────────────
        # If a companion .nfo exists this file was already organised by the
        # app (or compatible software).  We parse the NFO to pre-populate
        # metadata and flag the file so the UI can show it separately.
        # This is read-only — nothing about the video file is changed.
        nfo_path = p.with_suffix(".nfo")
        nfo_meta = _parse_nfo(nfo_path) if nfo_path.is_file() else None

        file_entry: dict = {
            "path":             str(p),
            "filename":         p.name,
            "size":             p.stat().st_size,
            "media_type":       det.media_type.value,
            "clean_name":       det.clean_name,
            "site":             det.site,
            "performers":       det.performers,
            "scene_title":      det.scene_title,
            "release_date":     det.release_date,
            "year":             det.year,
            "quality":          det.quality,
            "source":           det.source,
            "video_format":     det.video_format,
            "group":            det.group,
            # NFO-derived flags — None when no sidecar found
            "already_organized": nfo_meta is not None,
            "nfo_metadata":      nfo_meta,
        }
        files.append(file_entry)

    return {"count": len(files), "files": files}


# ─── Match Endpoint ────────────────────────────────────────────────────

def _stashdb_scene_to_dict(s) -> dict:
    """Convert a StashDBScene to the common scene dict used throughout the app."""
    return {
        "id": s.id,
        "title": s.title,
        "site": s.site,
        "network": s.network,
        "performers": s.performers,
        "release_date": s.release_date,
        "tags": s.tags,
        "poster_url": s.poster_url,
        "thumbnail_url": s.poster_url,  # StashDB doesn't separate thumb/poster
    }


_SITE_TITLE_RE = re.compile(
    r'^(?P<site>.+?)\s+[-–]\s+(?P<title>.+?)(?:\s*\(\d{4}[-./]\d{2}[-./]\d{2}\))?\s*$'
)
_DATE_SUFFIX_RE = re.compile(r'\s*\(\d{4}[-./]\d{2}[-./]\d{2}\)\s*$')


def _extract_site_title(file_data: dict) -> tuple[str | None, str]:
    """
    For files whose detector returned no site/title (e.g. "SiteName - Title (Date).mp4"),
    attempt to parse the components from clean_name.
    Returns (parsed_site, clean_title).
    """
    raw = file_data.get("scene_title") or file_data.get("clean_name", "")
    m = _SITE_TITLE_RE.match(raw)
    if m:
        site_part  = m.group("site").strip()
        title_part = _DATE_SUFFIX_RE.sub("", m.group("title")).strip()
        return site_part, title_part
    return None, raw


async def _match_one_stashdb(file_data: dict, sem: asyncio.Semaphore) -> dict:
    """Match a single file against StashDB using fingerprint then text search."""
    async with sem:
        no_match = {"original": file_data, "match": None, "confidence": 0, "alternatives": []}

        # ── 1. Fingerprint search (highest accuracy) ──────────────────
        file_path = Path(file_data.get("path", ""))
        if file_path.is_file():
            oshash = compute_oshash(file_path)
            # pHash computation is CPU-heavy; run it but cap at 20 s
            try:
                phash = await asyncio.wait_for(compute_phash_ffmpeg(file_path), timeout=20)
            except asyncio.TimeoutError:
                phash = None

            if oshash or phash:
                fp_results = await stashdb.find_by_fingerprint(oshash=oshash, phash=phash)
                if fp_results:
                    best = fp_results[0]
                    scene_dict = _stashdb_scene_to_dict(best)
                    confidence = adult_cascade_score(file_data, scene_dict)
                    alts = [_stashdb_scene_to_dict(s) for s in fp_results[1:5]]
                    return {
                        "original": file_data,
                        "match": scene_dict,
                        "confidence": round(max(confidence * 100, 90.0), 1),
                        "alternatives": alts,
                    }

        # ── 2. Pre-parse "Site - Title (Date)" format ─────────────────
        # Many files have no detector-parsed site/performers because the
        # filename follows "StudioName - Scene Title (YYYY-MM-DD)" which
        # doesn't match any detector pattern.  Extract those parts here
        # so search and scoring both have better inputs.
        parsed_site  = file_data.get("site")
        parsed_title = file_data.get("scene_title") or file_data.get("clean_name", "")

        if not parsed_site:
            parsed_site, parsed_title = _extract_site_title(file_data)

        # Build an enriched copy of file_data for cascade scoring
        scoring_data = dict(file_data)
        if parsed_site and not file_data.get("site"):
            scoring_data["site"] = parsed_site
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
            return no_match

        results_dicts = [_stashdb_scene_to_dict(s) for s in results[:5]]

        # ── 5. Score with enriched metadata ─────────────────────────
        best_result = find_best_match(scoring_data, results_dicts)
        if best_result:
            best_match, score = best_result
            return {
                "original": file_data,
                "match": best_match,
                "confidence": round(score * 100, 1),
                "alternatives": [r for r in results_dicts if r != best_match],
            }

        # ── 6. Title-only fallback ────────────────────────────────────
        # When no performer/date data exists, cascade scoring is near-zero
        # even for a correct match.  Compare just the title and accept if
        # it's a reasonable match (≥ 50% similarity).
        for r in results_dicts[:3]:
            title_sim = name_similarity(parsed_title, r.get("title", ""))
            if title_sim >= 0.50:
                return {
                    "original": file_data,
                    "match": r,
                    "confidence": round(title_sim * 80, 1),  # cap text-only at 80%
                    "alternatives": [x for x in results_dicts if x != r],
                }

        return no_match


@app.post("/api/match")
async def match_scenes(req: MatchRequest):
    """
    Match detected files against TPDB or StashDB.
    All files are matched concurrently (up to 5 at a time) to avoid rate limits.
    """
    use_stashdb = req.datasource == "stashdb"

    if use_stashdb:
        if not stashdb:
            raise HTTPException(
                status_code=503,
                detail="StashDB API key not configured. Set STASHDB_API_KEY environment variable."
            )
        sem = asyncio.Semaphore(5)
        matched_files = await asyncio.gather(*[_match_one_stashdb(f, sem) for f in req.files])
        return {"matches": list(matched_files)}

    # ── TPDB path (original behaviour) ────────────────────────────────
    if not tpdb:
        raise HTTPException(
            status_code=503,
            detail="TPDB API key not configured. Set TPDB_API_KEY environment variable."
        )

    # Semaphore: max 5 concurrent TPDB requests
    sem = asyncio.Semaphore(5)

    async def _match_one(file_data: dict) -> dict:
        async with sem:
            filename = file_data.get("filename", "")

            # Try automatic matching via filename parsing
            if req.auto_match:
                auto_match = await tpdb.parse_filename(filename)
                if auto_match:
                    # auto_match.network is already normalised to str|None by
                    # TPDBClient._parse_scene — use it directly.
                    net = auto_match.network or ""
                    _add_known_site(auto_match.site, net)
                    scene_dict = {
                        "id": auto_match.id,
                        "title": auto_match.title,
                        "site": auto_match.site,
                        "network": auto_match.network,  # plain str|None
                        "performers": auto_match.performers,
                        "release_date": auto_match.release_date,
                        "tags": auto_match.tags,
                        "poster_url": auto_match.poster_url_large,
                        "thumbnail_url": auto_match.thumbnail_url_small,
                    }
                    confidence = adult_cascade_score(file_data, scene_dict)
                    return {
                        "original": file_data,
                        "match": scene_dict,
                        "confidence": round(confidence * 100, 1),
                        "alternatives": [],
                    }

            # Fallback to search
            search_query = file_data.get("scene_title") or file_data.get("clean_name", "")
            site_filter  = file_data.get("site")
            search_results = await tpdb.search_scene(query=search_query, site=site_filter)

            if search_results:
                results_dicts = [
                    {
                        "id": s.id, "title": s.title, "site": s.site,
                        # s.network is already a plain str|None from _parse_scene
                        "network": s.network,
                        "performers": s.performers,
                        "release_date": s.release_date, "tags": s.tags,
                        "poster_url": s.poster_url_large,
                        "thumbnail_url": s.thumbnail_url_small,
                    }
                    for s in search_results[:5]
                ]
                best_result = find_best_match(file_data, results_dicts)
                if best_result:
                    best_match, score = best_result
                    return {
                        "original": file_data,
                        "match": best_match,
                        "confidence": round(score * 100, 1),
                        "alternatives": [r for r in results_dicts if r != best_match],
                    }
                return {"original": file_data, "match": None, "confidence": 0, "alternatives": results_dicts}

            return {"original": file_data, "match": None, "confidence": 0, "alternatives": []}

    # Run all matches concurrently, preserving original order
    matched_files = await asyncio.gather(*[_match_one(f) for f in req.files])
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
    return {"session_id": session_id}


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

    async def _match_one_sse(idx: int, file_data: dict) -> None:
        result = {"original": file_data, "match": None, "confidence": 0, "alternatives": []}
        try:
            if use_stashdb:
                result = await _match_one_stashdb(file_data, sem)
            else:
                async with sem:
                    filename = file_data.get("filename", "")
                    if auto_match:
                        auto = await tpdb.parse_filename(filename)
                        if auto:
                            net = auto.network or ""
                            _add_known_site(auto.site, net)
                            scene_dict = {
                                "id": auto.id, "title": auto.title, "site": auto.site,
                                "network": auto.network, "performers": auto.performers,
                                "release_date": auto.release_date, "tags": auto.tags,
                                "poster_url": auto.poster_url_large,
                                "thumbnail_url": auto.thumbnail_url_small,
                            }
                            confidence = adult_cascade_score(file_data, scene_dict)
                            result = {
                                "original": file_data, "match": scene_dict,
                                "confidence": round(confidence * 100, 1), "alternatives": [],
                            }
                            await q.put((idx, result))
                            return

                    search_query = file_data.get("scene_title") or file_data.get("clean_name", "")
                    site_filter  = file_data.get("site")
                    search_results = await tpdb.search_scene(query=search_query, site=site_filter)

                    if search_results:
                        results_dicts = [
                            {
                                "id": s.id, "title": s.title, "site": s.site,
                                "network": s.network, "performers": s.performers,
                                "release_date": s.release_date, "tags": s.tags,
                                "poster_url": s.poster_url_large,
                                "thumbnail_url": s.thumbnail_url_small,
                            }
                            for s in search_results[:5]
                        ]
                        best = find_best_match(file_data, results_dicts)
                        if best:
                            best_match, score = best
                            result = {
                                "original": file_data, "match": best_match,
                                "confidence": round(score * 100, 1),
                                "alternatives": [r for r in results_dicts if r != best_match],
                            }
                        else:
                            result = {"original": file_data, "match": None, "confidence": 0, "alternatives": results_dicts}
        except Exception:
            pass  # leave result as no-match on any transient error

        await q.put((idx, result))

    async def _event_stream():
        # Kick off all match tasks concurrently (semaphore limits parallelism)
        tasks = [asyncio.create_task(_match_one_sse(i, f)) for i, f in enumerate(files)]

        # Ordered slots so we can reconstruct the final list client-side
        ordered: list[dict | None] = [None] * total
        done_count = 0

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

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )


# ─── Preview-Paths Endpoint ──────────────────────────────────────────

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

        bindings = extract_template_vars(scene_data, file_data)
        new_path = build_new_path(old_path, tmpl, bindings)
        if flat:
            new_path = old_path.parent / new_path.name

        results.append({
            "old_path":       str(old_path),
            "new_path":       str(new_path),
            # True when the template produced no effective change
            "same_as_source": new_path.resolve() == old_path.resolve(),
            # True when the generated stem is blank / dot / dotdot
            "degenerate":     new_path.stem in ("", ".", ".."),
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
    phase1 = []   # list of (result, new_path, meta) tuples
    for operation in req.operations:
        old_path   = Path(operation["old_path"])
        scene_data = operation.get("scene_data", {})
        file_data  = operation.get("file_data", {})
        tmpl       = operation.get("template", TEMPLATES["site_date"])
        flat       = operation.get("flat", False)

        bindings = extract_template_vars(scene_data, file_data)
        new_path = build_new_path(old_path, tmpl, bindings)
        if flat:
            new_path = old_path.parent / new_path.name

        result = execute_rename(old_path, new_path, action)

        meta = {
            "title":        scene_data.get("title", ""),
            "site":         scene_data.get("site", ""),
            "performers":   scene_data.get("performers", []),
            "release_date": scene_data.get("release_date", ""),
            "tags":         scene_data.get("tags", []),
        }
        phase1.append((result, new_path, meta))

        if result.success and action != RenameAction.TEST:
            history.add_entry(old_path, new_path, action.value, True)

    # Serialize Phase 1 results (embed_warning is null — Phase 2 not run yet)
    phase1_results = [
        {
            "success":       result.success,
            "old_path":      str(result.old_path),
            "new_path":      str(result.new_path) if result.new_path else None,
            "action":        result.action.value,
            "error":         result.error,
            "embed_warning": None,
        }
        for result, _, _ in phase1
    ]

    # Test mode: no embedding happens, skip the background task.
    if action == RenameAction.TEST:
        return {"results": phase1_results}

    # ── Phase 2: metadata embedding + NFO — runs in background ──────────────
    embeddable = [(r, p, m) for r, p, m in phase1 if r.success and r.new_path]
    job_id = uuid.uuid4().hex
    _embed_jobs[job_id] = {
        "total":    len(embeddable),
        "done":     0,
        "warnings": [],
        "complete": len(embeddable) == 0,  # trivially complete if nothing to embed
        "created":  _time.monotonic(),
    }
    background_tasks.add_task(_run_embed_phase, job_id, embeddable)
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


async def _run_embed_phase(job_id: str, tasks: list) -> None:
    """
    Background task: embed metadata + write NFO for each successfully renamed file.
    Tasks are serialised through _EMBED_SEM (max 2 concurrent) so that NAS
    bandwidth is not saturated by parallel FFmpeg processes.
    Updates _embed_jobs[job_id] as work completes so the client can poll progress.
    """
    job = _embed_jobs.get(job_id)
    if job is None:
        return

    embed_sem = _get_embed_sem()

    async def _embed_one(result, new_path, meta):
        async with embed_sem:
            warning = None
            ok, err = await embed_metadata(result.new_path, meta)
            if not ok:
                warning = f"Metadata embedding warning: {err}"
            try:
                write_nfo(result.new_path, meta)
            except Exception as nfo_err:
                if not warning:
                    warning = f"NFO write warning: {nfo_err}"
            job["done"] += 1
            if warning:
                job["warnings"].append({"path": str(result.new_path), "warning": warning})

    await asyncio.gather(*[_embed_one(r, p, m) for r, p, m in tasks])
    job["complete"] = True


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

    job = _embed_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Embed job not found or expired")

    return {
        "job_id":   job_id,
        "total":    job["total"],
        "done":     job["done"],
        "complete": job["complete"],
        "warnings": job["warnings"],
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
    }


class SaveSettingsRequest(BaseModel):
    tpdb_api_key:    Optional[str] = None
    stashdb_api_key: Optional[str] = None

    @field_validator("tpdb_api_key", "stashdb_api_key", mode="before")
    @classmethod
    def _strip_and_cap(cls, v) -> Optional[str]:
        if v is None:
            return None
        v = str(v).strip()
        if len(v) > _SETTINGS_KEY_MAX_LEN:
            raise ValueError(f"API key exceeds maximum length ({_SETTINGS_KEY_MAX_LEN})")
        return v or None   # normalise empty string → None


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
    changed  = []

    for field_name, env_var, settings_key, label in [
        ("tpdb_api_key",    "TPDB_API_KEY",    "tpdb_api_key",    "TPDB"),
        ("stashdb_api_key", "STASHDB_API_KEY", "stashdb_api_key", "StashDB"),
    ]:
        new_val = getattr(req, field_name)

        # Env var takes precedence — silently ignore UI attempts to change it.
        if os.getenv(env_var, "").strip():
            continue

        if new_val:          # non-empty → update saved value
            settings[settings_key] = new_val
            changed.append(label)
        # blank → leave existing saved value untouched

    if changed:
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
        "tpdb":    {"active": tpdb    is not None, "source": tpdb_src},
        "stashdb": {"active": stashdb is not None, "source": stashdb_src},
    }


# ─── History Endpoints ─────────────────────────────────────────────────

@app.get("/api/history")
async def get_history(limit: int = Query(50, ge=1, le=500)):
    """
    Get rename history.
    """
    entries = history.get_recent(limit)
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
            }
            for e in entries
        ]
    }


@app.post("/api/history/undo")
async def undo_rename():
    """
    Undo the last rename operation.
    """
    entry = history.undo_last()
    if entry:
        return {
            "success": True,
            "undone": {
                "old_path": entry.old_path,
                "new_path": entry.new_path,
            }
        }
    else:
        return {
            "success": False,
            "error": "No operations to undo"
        }


# ─── Browse Endpoint ───────────────────────────────────────────────────

@app.get("/api/browse")
async def browse_directory(path: str = Query("/media")):
    """
    Browse directories on the server.
    Returns list of subdirectories and files.
    """
    try:
        p = Path(path).resolve()

        # Security: reject anything outside the explicitly allowed mount points.
        # Path("/") is intentionally excluded — it would allow traversal to any
        # file on the filesystem (e.g. /etc/passwd, /root/.ssh).
        if not _is_allowed_path(p):
            raise HTTPException(status_code=403, detail="Access denied to this path")
        
        if not p.exists():
            raise HTTPException(status_code=404, detail="Path does not exist")
        
        if not p.is_dir():
            raise HTTPException(status_code=400, detail="Path is not a directory")
        
        items = []
        
        # Add parent directory link
        if p != p.parent:
            items.append({
                "name": "..",
                "path": str(p.parent),
                "type": "directory",
                "size": 0,
            })
        
        # List directory contents
        for item in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
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
        }

    except HTTPException:
        raise  # pass 403/404/400 through unchanged
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Templates Endpoint ────────────────────────────────────────────────

@app.get("/api/templates")
async def get_templates():
    """
    Get available naming templates.
    """
    return {"templates": TEMPLATES}


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


USER_TAGS_FILE = DATA_DIR / "user_tags.json"

def _load_user_tags() -> list[str]:
    try:
        return json.loads(USER_TAGS_FILE.read_text())
    except Exception:
        return []

def _save_user_tags(tags: list[str]) -> None:
    USER_TAGS_FILE.write_text(json.dumps(sorted(tags)))

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
    user_tags = _load_user_tags()
    if req.tag not in user_tags:
        if len(user_tags) >= _MAX_USER_TAGS:
            raise HTTPException(status_code=400, detail=f"Tag limit ({_MAX_USER_TAGS}) reached")
        user_tags.append(req.tag)
        _save_user_tags(user_tags)
    return {"ok": True, "tag": req.tag}

@app.delete("/api/tags/{tag}")
async def delete_user_tag(tag: str):
    """Remove a user-created tag."""
    user_tags = _load_user_tags()
    user_tags = [t for t in user_tags if t != tag]
    _save_user_tags(user_tags)
    return {"ok": True}

KNOWN_SITES_FILE = DATA_DIR / "known_sites.json"

def _load_known_sites() -> list[dict]:
    try:
        return json.loads(KNOWN_SITES_FILE.read_text())
    except Exception:
        return []

def _save_known_sites(sites: list[dict]) -> None:
    KNOWN_SITES_FILE.write_text(json.dumps(sites))

def _add_known_site(name: str, network: str = "") -> None:
    if not name:
        return
    sites = _load_known_sites()
    if not any(s["name"] == name for s in sites):
        sites.append({"name": name, "network": network})
        sites.sort(key=lambda s: s["name"].lower())
        _save_known_sites(sites)

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
    # Cache any new ones found — s.network is str|None from _parse_site()
    for s in results:
        _add_known_site(s.name, s.network or "")
    return {"sites": sites}


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


@app.post("/api/extract-thumbnails")
async def extract_thumbnails(request: ThumbnailRequest):
    """
    Extract multiple thumbnails from a video file using ffmpeg.
    Returns base64-encoded images for preview.
    """
    file_path = Path(request.file_path)

    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Video file not found")
    
    try:
        # Get video duration first
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(file_path)
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=10)
        duration = float(result.stdout.strip())
        
        # Generate 6 thumbnails at different timestamps
        thumbnails = []
        thumbnail_dir = DATA_DIR / "thumbnails" / file_path.stem
        thumbnail_dir.mkdir(parents=True, exist_ok=True)
        
        # Extract thumbnails at 10%, 25%, 40%, 55%, 70%, 85% of duration
        percentages = [0.10, 0.25, 0.40, 0.55, 0.70, 0.85]
        
        for idx, pct in enumerate(percentages):
            timestamp = duration * pct
            thumb_path = thumbnail_dir / f"thumb_{idx}.jpg"
            
            # Extract frame with ffmpeg
            ffmpeg_cmd = [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
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
    }
    try:
        write_nfo(file_path, meta)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"NFO write failed: {e}")

    nfo_path = file_path.with_suffix(".nfo")
    return {"success": True, "nfo_path": str(nfo_path)}


# ─── Metadata embedding helper ──────────────────────────────────────────

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
    year         = release_date[:4] if release_date else ""

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
    if description:
        ET.SubElement(root, "plot").text       = description
        ET.SubElement(root, "outline").text    = description
    ET.SubElement(root, "genre").text          = "Adult"
    for tag in tags:
        ET.SubElement(root, "tag").text        = tag
    for performer in performers:
        actor_el = ET.SubElement(root, "actor")
        ET.SubElement(actor_el, "name").text   = performer

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
        description → description / comment fallback
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
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
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

        def _add(key: str, value: str):
            if value:
                cmd.extend(["-metadata", f"{key}={value}"])

        _add("title",       metadata.get("title", ""))
        _add("artist",      ", ".join(metadata.get("performers", [])))
        _add("album",       metadata.get("site", ""))
        _add("date",        metadata.get("release_date", ""))
        _add("comment",     ", ".join(metadata.get("tags", [])))

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


@app.post("/api/save-manual-metadata")
async def save_manual_metadata(req: ManualMetadataRequest):
    """
    Embed manually entered metadata directly into the video file via FFmpeg.
    No sidecar JSON file is written.
    """
    file_path = Path(req.file_path)

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    metadata = {
        "title":        req.title,
        "site":         req.site or "",
        "performers":   req.performers,
        "release_date": req.release_date or "",
        "tags":         req.tags,
    }

    ok, err = await embed_metadata(file_path, metadata)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Metadata embedding failed: {err}")

    try:
        write_nfo(file_path, metadata)
    except Exception:
        pass

    # Optionally mark a preferred thumbnail
    thumbnail_url = None
    if req.thumbnail_index is not None:
        thumbnail_dir = DATA_DIR / "thumbnails" / file_path.stem
        source_thumb = thumbnail_dir / f"thumb_{req.thumbnail_index}.jpg"
        if source_thumb.exists():
            dest_thumb = thumbnail_dir / "selected.jpg"
            import shutil
            shutil.copy2(source_thumb, dest_thumb)
            thumbnail_url = f"/api/thumbnail/{file_path.stem}/selected.jpg"

    return {
        "success": True,
        "thumbnail_url": thumbnail_url,
        "metadata": metadata,
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


# ─── Health Check ──────────────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """
    Health check endpoint.
    """
    privacy_mode = os.getenv("PRIVACY_MODE", "true").lower() == "true"
    return {
        "status": "healthy",
        "tpdb_configured": tpdb is not None,
        "privacy_mode": privacy_mode,
    }
