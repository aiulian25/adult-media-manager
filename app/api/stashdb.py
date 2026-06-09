"""
StashDB GraphQL API client.
StashDB is a free, community-driven adult content metadata database.

Registration: https://stashdb.org/register
  - Invite code (public): 3bf7c4b8-b7a6-45b8-a8a6-8b38c10b8fa6
    (replaced periodically; check https://guidelines.stashdb.org/docs/faq_getting-started/stashdb/accessing-stashdb/)
  - After registering, log in → click your username → copy the API key.

GraphQL endpoint: https://stashdb.org/graphql
Auth header: ApiKey: <your_key>
"""

import os
import struct
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

GRAPHQL_ENDPOINT = "https://stashdb.org/graphql"


# ─── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class StashDBScene:
    id: str
    title: str
    site: Optional[str] = None
    network: Optional[str] = None
    performers: list[str] = field(default_factory=list)
    release_date: Optional[str] = None   # YYYY-MM-DD
    duration: Optional[int] = None       # seconds
    tags: list[str] = field(default_factory=list)
    poster_url: Optional[str] = None


# ─── Queries ───────────────────────────────────────────────────────────────────

_SCENE_FIELDS = """
  id
  title
  date
  duration
  studio { name parent { name } }
  performers { performer { name } }
  tags { name }
  images { url width }
"""

_Q_SEARCH = f"""
query SearchScene($term: String!) {{
  searchScene(term: $term) {{
    {_SCENE_FIELDS}
  }}
}}
"""

_Q_FINGERPRINTS = f"""
query FindByFingerprints($fingerprints: [[FingerprintQueryInput!]!]!) {{
  findScenesBySceneFingerprints(fingerprints: $fingerprints) {{
    {_SCENE_FIELDS}
  }}
}}
"""


# ─── OSHash (pure Python, no extra deps) ───────────────────────────────────────

def compute_oshash(file_path: Path) -> Optional[str]:
    """
    Compute the OpenSubtitles/OSHash fingerprint of a file.
    StashDB stores this under algorithm=OSHASH.
    Returns a 16-character lowercase hex string, or None on error.
    """
    try:
        chunk = 65536  # 64 KB
        file_size = file_path.stat().st_size
        if file_size == 0:
            return None

        hash_val = file_size
        with open(file_path, "rb") as f:
            # Head chunk
            head = f.read(min(chunk, file_size))
            for i in range(0, len(head) - 7, 8):
                hash_val += struct.unpack_from("<Q", head, i)[0]

            # Tail chunk (only if file is large enough)
            if file_size > chunk:
                f.seek(-chunk, 2)
                tail = f.read(chunk)
                for i in range(0, len(tail) - 7, 8):
                    hash_val += struct.unpack_from("<Q", tail, i)[0]

        return format(hash_val & 0xFFFFFFFFFFFFFFFF, "016x")
    except OSError:
        return None


# ─── pHash via ffmpeg ──────────────────────────────────────────────────────────

async def compute_phash_ffmpeg(file_path: Path) -> Optional[str]:
    """
    Compute a perceptual hash (pHash) of a video by extracting a frame
    at ~20% through the video and applying a standard DCT-based pHash.

    Returns the hash as a decimal integer string (StashDB's expected format),
    or None on error.  Requires ffmpeg in PATH.
    """
    try:
        import math

        # ── Step 1: get duration from ffprobe ────────────────────────────
        probe_cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_entries", "format=duration",
            str(file_path),
        ]
        probe = await asyncio.create_subprocess_exec(
            *probe_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        probe_out, _ = await asyncio.wait_for(probe.communicate(), timeout=15)
        if probe.returncode != 0:
            return None

        import json as _json
        info = _json.loads(probe_out)
        duration = float(info.get("format", {}).get("duration", 0))
        if duration <= 0:
            return None
        seek = duration * 0.2  # 20% in

        # ── Step 2: extract one 32×32 greyscale frame ────────────────────
        frame_cmd = [
            "ffmpeg", "-ss", str(seek), "-i", str(file_path),
            "-vframes", "1",
            "-vf", "scale=32:32:force_original_aspect_ratio=disable,format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray",
            "pipe:1",
        ]
        ff = await asyncio.create_subprocess_exec(
            *frame_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        raw, _ = await asyncio.wait_for(ff.communicate(), timeout=30)
        if ff.returncode != 0 or len(raw) < 1024:
            return None

        pixels = list(raw[:1024])  # exactly 32×32

        # ── Step 3: DCT-based pHash ──────────────────────────────────────
        # Build 32×32 float array
        n = 32
        img = [[float(pixels[r * n + c]) for c in range(n)] for r in range(n)]

        # Apply 2D DCT (type-II)
        dct = [[0.0] * n for _ in range(n)]
        for u in range(n):
            cu = (math.sqrt(1 / n) if u == 0 else math.sqrt(2 / n))
            for v in range(n):
                cv = (math.sqrt(1 / n) if v == 0 else math.sqrt(2 / n))
                s = 0.0
                for x in range(n):
                    for y in range(n):
                        s += img[x][y] * math.cos(math.pi * u * (2 * x + 1) / (2 * n)) \
                                       * math.cos(math.pi * v * (2 * y + 1) / (2 * n))
                dct[u][v] = cu * cv * s

        # Use top-left 8×8 (excluding DC term at [0][0])
        low = [dct[u][v] for u in range(8) for v in range(8)]
        low_no_dc = low[1:]  # skip DC
        median_val = sorted(low_no_dc)[len(low_no_dc) // 2]

        # Binary hash: 64 bits from the full 8×8 block
        bits = [1 if v >= median_val else 0 for v in low]
        hash_int = 0
        for bit in bits:
            hash_int = (hash_int << 1) | bit

        return str(hash_int)  # StashDB stores pHash as decimal integer string

    except Exception:
        return None


# ─── Client ────────────────────────────────────────────────────────────────────

class StashDBClient:
    """
    Minimal async GraphQL client for StashDB.

    Authentication: pass the API key from your StashDB user profile as
    STASHDB_API_KEY environment variable.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("STASHDB_API_KEY")
        if not self.api_key:
            raise ValueError(
                "StashDB API key required. Set STASHDB_API_KEY environment variable. "
                "Register free at https://stashdb.org/register "
                "(invite code: 3bf7c4b8-b7a6-45b8-a8a6-8b38c10b8fa6)"
            )
        self._client = httpx.AsyncClient(
            headers={
                "ApiKey": self.api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Adult-Media-Manager/1.0",
            },
            timeout=30.0,
        )

    async def close(self):
        await self._client.aclose()

    # ── low-level ──────────────────────────────────────────────────────

    async def _query(self, query: str, variables: dict) -> dict:
        resp = await self._client.post(
            GRAPHQL_ENDPOINT,
            json={"query": query, "variables": variables},
        )
        resp.raise_for_status()
        body = resp.json()
        if "errors" in body:
            raise RuntimeError(f"StashDB GraphQL error: {body['errors']}")
        return body.get("data", {})

    # ── public ─────────────────────────────────────────────────────────

    async def search_scene(
        self,
        query: str,
        performer: Optional[str] = None,
        studio: Optional[str] = None,
    ) -> list[StashDBScene]:
        """Text search — returns up to 10 results."""
        term = query
        if performer:
            term = f"{performer} {term}"
        if studio:
            term = f"{studio} {term}"
        try:
            data = await self._query(_Q_SEARCH, {"term": term})
            return [self._parse(s) for s in data.get("searchScene", [])]
        except Exception as exc:
            print(f"StashDB search error: {exc}")
            return []

    async def find_by_fingerprint(
        self,
        oshash: Optional[str] = None,
        phash: Optional[str] = None,
        duration: Optional[int] = None,
    ) -> list[StashDBScene]:
        """
        Fingerprint lookup — exact match, highest accuracy.
        Pass at least one of oshash or phash.
        findScenesBySceneFingerprints takes [[FingerprintQueryInput]] —
        one inner list per scene query; we pass one inner list with all
        our fingerprints and get back one inner result list.
        """
        fingerprints = []
        if oshash:
            fingerprints.append({"algorithm": "OSHASH", "hash": oshash})
        if phash:
            fingerprints.append({"algorithm": "PHASH", "hash": phash})
        if not fingerprints:
            return []
        try:
            # Outer list = one query group; inner list = fingerprints for that group
            data = await self._query(_Q_FINGERPRINTS, {"fingerprints": [fingerprints]})
            # Returns [[Scene, ...]] — one inner list per query group
            results_nested = data.get("findScenesBySceneFingerprints", [[]])
            flat = [scene for group in results_nested for scene in (group or [])]
            return [self._parse(s) for s in flat]
        except Exception as exc:
            print(f"StashDB fingerprint error: {exc}")
            return []

    # ── parsing ────────────────────────────────────────────────────────

    @staticmethod
    def _parse(data: dict) -> StashDBScene:
        studio = data.get("studio") or {}
        site = studio.get("name") or ""
        parent = (studio.get("parent") or {}).get("name") or None

        performers = [
            a["performer"]["name"]
            for a in data.get("performers", [])
            if a.get("performer", {}).get("name")
        ]
        tags = [t["name"] for t in data.get("tags", []) if t.get("name")]

        # Pick best image: prefer widest
        images = sorted(data.get("images", []), key=lambda i: i.get("width", 0), reverse=True)
        poster_url = images[0]["url"] if images else None

        return StashDBScene(
            id=data.get("id", ""),
            title=data.get("title") or "",
            site=site or None,
            network=parent,
            performers=performers,
            release_date=data.get("date"),
            duration=data.get("duration"),
            tags=tags,
            poster_url=poster_url,
        )
