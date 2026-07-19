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
import json
import struct
import asyncio
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from app.core.tools import ffmpeg_path, ffprobe_path

GRAPHQL_ENDPOINT = "https://stashdb.org/graphql"


# ─── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class StashDBScene:
    id: str
    title: str
    site: Optional[str] = None
    network: Optional[str] = None
    performers: list[str] = field(default_factory=list)
    # Aligned 1:1 with `performers`: normalized lowercase gender per performer
    # ("female", "male", "transgender_female", …) or None when StashDB doesn't
    # state it. Drives the ♀-first ordering; never rendered as-is.
    performer_genders: list = field(default_factory=list)
    release_date: Optional[str] = None   # YYYY-MM-DD
    duration: Optional[int] = None       # seconds
    tags: list[str] = field(default_factory=list)
    poster_url: Optional[str] = None
    description: Optional[str] = None     # scene synopsis (GraphQL "details")
    # F6 — data StashDB always had but we never requested:
    code: Optional[str] = None            # studio's canonical scene code
    director: Optional[str] = None
    url: Optional[str] = None             # first scene URL (studio page)
    # Per-scene performer credits: [{"name": canonical, "as": credited-alias|None}].
    # "as" is the alias the performer was credited under IN THIS SCENE — the
    # alias↔canonical pair the learned-alias table otherwise pays TPDB for.
    performer_credits: list[dict] = field(default_factory=list)
    # F7: fanart backdrop — the second-widest image (the widest is the poster).
    fanart_url: Optional[str] = None


# ─── Queries ───────────────────────────────────────────────────────────────────

_SCENE_FIELDS = """
  id
  title
  details
  date
  duration
  code
  director
  urls { url }
  studio { name parent { name } }
  performers { as performer { name gender } }
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

_Q_FIND_SCENE = f"""
query FindScene($id: ID!) {{
  findScene(id: $id) {{
    {_SCENE_FIELDS}
  }}
}}
"""

# F5 — fingerprint contribution (OPT-IN, default off). Shape verified against
# the live stash-box schema by introspection (2026-07-19):
#   FingerprintSubmission { scene_id: ID!, fingerprint: FingerprintInput!,
#                           unmatch: Boolean, vote: FingerprintSubmissionType }
#   FingerprintInput { hash: FingerprintHash!, algorithm: MD5|OSHASH|PHASH,
#                      duration: Int! }   ← duration is REQUIRED
# One fingerprint per submission — OSHASH and PHASH are two separate calls.
_M_SUBMIT_FP = """
mutation SubmitFingerprint($input: FingerprintSubmission!) {
  submitFingerprint(input: $input)
}
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

# Which pHash algorithm to compute (F14):
#   "sprite" (default) — stash-compatible videophash: a 5×5 sprite of 25 frames
#       across the runtime, hashed like goimagehash.PerceptionHash. Matches the
#       PHASH fingerprints StashDB actually stores (submitted by stash users),
#       so re-encodes hit the fingerprint index. Serialized as lowercase hex —
#       the exact stash-box wire format (stash utils.PhashToString).
#   "frame" — the legacy single-frame hash (decimal string), byte-for-byte
#       today's behavior, for catalogs that must stay self-consistent.
# A deployment-layer knob (like AMM_SCAN_PHASH); identical on every target.
_PHASH_ALGO: str = os.getenv("AMM_PHASH_ALGO", "sprite").strip().lower()

# Match-path budget for one pHash computation: the sprite needs 25 sequential
# ffmpeg seeks (slow on NAS mounts); the legacy single frame needs one.
PHASH_MATCH_TIMEOUT: int = 90 if _PHASH_ALGO == "sprite" else 20

# The ffmpeg frame filter that defines the pHash *input pixels*. Both the async
# extractor (match path) and the sync one (scan path) MUST use this identical
# filter: it fully determines the 32×32 greyscale bytes fed to
# _phash_from_gray_frame, so changing it silently changes the meaning of every
# stored pHash. Keeping it in one constant is what guarantees a scan-computed
# pHash equals a match-computed one for the same file (no drift between paths).
_PHASH_FRAME_VF = "scale=32:32:force_original_aspect_ratio=disable,format=gray"
# Seek point (fraction through the video) for the sampled frame — shared too.
_PHASH_SEEK_FRACTION = 0.2


def _phash_from_gray_frame(raw: bytes) -> Optional[str]:
    """
    Compute a 64-bit DCT perceptual hash from a raw 32×32 greyscale frame.

    Pure CPU work, factored out of compute_phash_ffmpeg so it can be run via
    asyncio.to_thread and never block the event loop.

    Two optimisations vs. the previous inline implementation, both of which
    leave the output **bit-identical** for the same input bytes:
      • Only the top-left 8×8 DCT block is used for the hash, so we compute just
        those 64 coefficients instead of the full 32×32 matrix (~16× less work).
        The discarded coefficients never influenced the result.
      • The cosine factors are precomputed once (8 frequencies × 32 samples =
        256 cosines) instead of being recomputed inside the innermost loop
        (~2 million math.cos calls before).
    The per-term multiply order (img · cos_u · cos_v) and the x→y accumulation
    order are preserved exactly, so floating-point results match the old code.
    """
    import math

    n = 32          # frame is 32×32
    k = 8           # only the top-left 8×8 DCT block is needed for the hash
    if len(raw) < n * n:
        return None
    pixels = raw[:n * n]
    img = [[float(pixels[r * n + c]) for c in range(n)] for r in range(n)]

    # cos_t[f][i] = cos(pi * f * (2i+1) / (2n)); same formula on both axes.
    cos_t = [
        [math.cos(math.pi * f * (2 * i + 1) / (2 * n)) for i in range(n)]
        for f in range(k)
    ]

    # 2D DCT-II restricted to the 8×8 low-frequency block.
    dct = [[0.0] * k for _ in range(k)]
    for u in range(k):
        cu = math.sqrt(1 / n) if u == 0 else math.sqrt(2 / n)
        cos_u = cos_t[u]
        for v in range(k):
            cv = math.sqrt(1 / n) if v == 0 else math.sqrt(2 / n)
            cos_v = cos_t[v]
            s = 0.0
            for x in range(n):
                row = img[x]
                cux = cos_u[x]
                for y in range(n):
                    s += row[y] * cux * cos_v[y]
            dct[u][v] = cu * cv * s

    low = [dct[u][v] for u in range(k) for v in range(k)]
    low_no_dc = low[1:]  # skip DC term at [0][0]
    median_val = sorted(low_no_dc)[len(low_no_dc) // 2]

    # Binary hash: 64 bits from the full 8×8 block.
    bits = [1 if val >= median_val else 0 for val in low]
    hash_int = 0
    for bit in bits:
        hash_int = (hash_int << 1) | bit

    # Legacy (frame-mode) format: decimal string. Kept byte-for-byte for
    # AMM_PHASH_ALGO=frame; the stash-compatible sprite hash (F14) uses the
    # hex wire format StashDB actually expects.
    return str(hash_int)


# ─── Stash-compatible videophash (sprite pHash, F14) ──────────────────────────
# A constant-for-constant port of stash's pkg/hash/videophash pipeline — the
# code that produced every PHASH fingerprint stored on StashDB:
#   1. 25 screenshots at t = 0.05·dur + i·(0.9·dur/25), scaled `scale=160:-2`
#      (fast seek: -ss before -i), 5×5 montage pasted row-major.
#   2. goimagehash.PerceptionHash over the montage: nfnt/resize Bilinear to
#      64×64 (two-pass transposed filtering with int16-quantized triangle
#      weights and integer division — ported exactly), gray = .299R+.587G+.114B,
#      UNSCALED DCT-II (rows then columns), top-left 8×8 incl. DC, upper median
#      (sorted index 32 of 64), bit set when coeff > median, MSB first.
#   3. Hex serialization (stash utils.PhashToString: FormatUint(u64, 16)).
# Pure Python + ffmpeg — no numpy/PIL, identical on every build target.

_SPRITE_COLUMNS = 5
_SPRITE_ROWS = 5
_SPRITE_CHUNKS = _SPRITE_COLUMNS * _SPRITE_ROWS
_SPRITE_SCREENSHOT_WIDTH = 160


def _videophash_timestamps(duration: float) -> list[float]:
    """Stash's sprite sample times: 5% margins, 25 steps across the middle 90%."""
    offset = 0.05 * duration
    step = (0.9 * duration) / _SPRITE_CHUNKS
    return [offset + i * step for i in range(_SPRITE_CHUNKS)]


def _sprite_frame_cmd(file_path: Path, seek: float) -> list[str]:
    """One sprite screenshot: fast seek, width 160, aspect kept (even height).

    Raw rgb24 out — pixel-identical to stash's BMP round trip (both come from
    the same swscale `scale=160:-2` output; BMP is lossless).
    """
    return [
        ffmpeg_path(), "-ss", str(seek), "-i", str(file_path),
        "-vframes", "1",
        "-vf", f"scale={_SPRITE_SCREENSHOT_WIDTH}:-2",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "pipe:1",
    ]


def _nfnt_bilinear_weights(out_size: int, in_size: int):
    """nfnt/resize createWeights8 for the Bilinear (triangle, taps=2) kernel.

    Returns (coeffs, starts, filter_length) with the library's exact int16
    quantization (kernel·256 truncated) so borderline pixels round identically.
    """
    import math
    scale = in_size / out_size
    filter_length = 2 * max(int(math.ceil(scale)), 1)
    filter_factor = min(1.0 / scale, 1.0)
    coeffs: list[list[int]] = []
    starts: list[int] = []
    for y in range(out_size):
        interp = scale * (y + 0.5) - 0.5
        start = int(interp) - filter_length // 2 + 1
        frac = interp - start
        row = []
        for i in range(filter_length):
            x = abs((frac - i) * filter_factor)
            k = (1.0 - x) if x <= 1.0 else 0.0
            row.append(int(k * 256))
        coeffs.append(row)
        starts.append(start)
    return coeffs, starts, filter_length


def _videophash_from_tiles(frames: list[bytes], tile_w: int, tile_h: int) -> Optional[str]:
    """Assemble the 5×5 montage, resize to 64×64, gray, DCT, median-hash (hex)."""
    if len(frames) != _SPRITE_CHUNKS or tile_w <= 0 or tile_h <= 0:
        return None
    need = tile_w * tile_h * 3
    if any(len(f) < need for f in frames):
        return None

    # Montage rows (row-major paste: x = w·(i%5), y = h·(i//5)), 800×(5·tile_h).
    in_w = tile_w * _SPRITE_COLUMNS
    in_h = tile_h * _SPRITE_ROWS
    rows: list[bytes] = []
    for j in range(in_h):
        tr, y = divmod(j, tile_h)
        rows.append(b"".join(
            frames[tr * _SPRITE_COLUMNS + c][y * tile_w * 3:(y + 1) * tile_w * 3]
            for c in range(_SPRITE_COLUMNS)
        ))

    # Pass 1 (horizontal, output transposed): temp[x_out][row] = filtered RGB.
    coeffs, starts, flen = _nfnt_bilinear_weights(64, in_w)
    max_x = in_w - 1
    temp = [[(0, 0, 0)] * in_h for _ in range(64)]
    for r in range(in_h):
        row = rows[r]
        for xo in range(64):
            cs = coeffs[xo]
            st = starts[xo]
            a0 = a1 = a2 = s = 0
            for i in range(flen):
                c = cs[i]
                if c:
                    xi = st + i
                    if xi < 0:
                        xi = 0
                    elif xi > max_x:
                        xi = max_x
                    o = xi * 3
                    a0 += c * row[o]
                    a1 += c * row[o + 1]
                    a2 += c * row[o + 2]
                    s += c
            temp[xo][r] = (a0 // s, a1 // s, a2 // s)

    # Pass 2 (vertical): gray64[y][x] straight from the filtered RGB.
    coeffs, starts, flen = _nfnt_bilinear_weights(64, in_h)
    max_y = in_h - 1
    gray = [[0.0] * 64 for _ in range(64)]
    for x in range(64):
        col = temp[x]
        for yo in range(64):
            cs = coeffs[yo]
            st = starts[yo]
            a0 = a1 = a2 = s = 0
            for i in range(flen):
                c = cs[i]
                if c:
                    yi = st + i
                    if yi < 0:
                        yi = 0
                    elif yi > max_y:
                        yi = max_y
                    px = col[yi]
                    a0 += c * px[0]
                    a1 += c * px[1]
                    a2 += c * px[2]
                    s += c
            gray[yo][x] = (0.299 * (a0 // s) + 0.587 * (a1 // s)
                           + 0.114 * (a2 // s))

    # Unscaled DCT-II (goimagehash DCT2DFast64 semantics): rows along x, then
    # columns along y; keep the top-left 8×8 block INCLUDING the DC term, laid
    # out flattens[8·yfreq + xfreq].
    import math
    cos_t = [[math.cos(math.pi * (2 * n + 1) * k / 128.0) for n in range(64)]
             for k in range(8)]
    row_freq = [[0.0] * 8 for _ in range(64)]
    for y in range(64):
        g = gray[y]
        for k in range(8):
            ck = cos_t[k]
            row_freq[y][k] = sum(g[n] * ck[n] for n in range(64))
    flattens = [0.0] * 64
    for i in range(8):          # width frequency
        for j in range(8):      # height frequency
            cj = cos_t[j]
            flattens[8 * j + i] = sum(row_freq[y][i] * cj[y] for y in range(64))

    # Upper median of all 64 (quickselect at index len/2), strict > sets bits.
    median = sorted(flattens)[32]
    h = 0
    for idx, p in enumerate(flattens):
        if p > median:
            h |= 1 << (63 - idx)
    return format(h, "x")   # stash-box wire format: lowercase hex, no padding


def compute_videophash_sync(file_path: Path) -> Optional[str]:
    """Stash-compatible sprite pHash (sync, scan path). None on any failure."""
    try:
        probe = subprocess.run(
            [ffprobe_path(), "-v", "quiet", "-print_format", "json",
             "-show_entries", "format=duration", str(file_path)],
            capture_output=True, timeout=15,
        )
        if probe.returncode != 0:
            return None
        duration = float(json.loads(probe.stdout or b"{}")
                         .get("format", {}).get("duration", 0))
        if duration <= 0:
            return None

        frames: list[bytes] = []
        tile_w = _SPRITE_SCREENSHOT_WIDTH
        tile_h = 0
        for seek in _videophash_timestamps(duration):
            ff = subprocess.run(_sprite_frame_cmd(file_path, seek),
                                capture_output=True, timeout=30)
            raw = ff.stdout
            if ff.returncode != 0 or len(raw) < tile_w * 2 * 3:
                return None
            if tile_h == 0:
                tile_h = len(raw) // (tile_w * 3)
            frames.append(raw)
        return _videophash_from_tiles(frames, tile_w, tile_h)
    except Exception:
        return None


async def compute_videophash(file_path: Path) -> Optional[str]:
    """Stash-compatible sprite pHash (async, match path). None on any failure."""
    try:
        probe = await asyncio.create_subprocess_exec(
            ffprobe_path(), "-v", "quiet", "-print_format", "json",
            "-show_entries", "format=duration", str(file_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        probe_out, _ = await asyncio.wait_for(probe.communicate(), timeout=15)
        if probe.returncode != 0:
            return None
        duration = float(json.loads(probe_out or b"{}")
                         .get("format", {}).get("duration", 0))
        if duration <= 0:
            return None

        frames: list[bytes] = []
        tile_w = _SPRITE_SCREENSHOT_WIDTH
        tile_h = 0
        for seek in _videophash_timestamps(duration):
            ff = await asyncio.create_subprocess_exec(
                *_sprite_frame_cmd(file_path, seek),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            raw, _ = await asyncio.wait_for(ff.communicate(), timeout=30)
            if ff.returncode != 0 or len(raw) < tile_w * 2 * 3:
                return None
            if tile_h == 0:
                tile_h = len(raw) // (tile_w * 3)
            frames.append(raw)
        # The montage/DCT math is pure CPU — keep it off the event loop.
        return await asyncio.to_thread(_videophash_from_tiles, frames, tile_w, tile_h)
    except Exception:
        return None


async def compute_phash_ffmpeg(file_path: Path) -> Optional[str]:
    """pHash dispatcher (F14): sprite (stash-compatible, default) or legacy frame.

    Kept under the historical name so both call sites (scan/match) stay
    untouched; ``AMM_PHASH_ALGO=frame`` restores the old behavior byte-for-byte.
    """
    if _PHASH_ALGO == "frame":
        return await _compute_frame_phash(file_path)
    return await compute_videophash(file_path)


def compute_phash_ffmpeg_sync(file_path: Path) -> Optional[str]:
    """Sync pHash dispatcher (F14) — see :func:`compute_phash_ffmpeg`."""
    if _PHASH_ALGO == "frame":
        return _compute_frame_phash_sync(file_path)
    return compute_videophash_sync(file_path)


async def _compute_frame_phash(file_path: Path) -> Optional[str]:
    """
    Compute a perceptual hash (pHash) of a video by extracting a frame
    at ~20% through the video and applying a standard DCT-based pHash.

    Legacy algorithm (AMM_PHASH_ALGO=frame): single frame, decimal string —
    kept byte-for-byte so pre-F14 catalogs stay self-consistent.
    Returns the hash string, or None on error.  Requires ffmpeg in PATH.
    """
    try:
        # ── Step 1: get duration from ffprobe ────────────────────────────
        probe_cmd = [
            ffprobe_path(), "-v", "quiet", "-print_format", "json",
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

        info = json.loads(probe_out)
        duration = float(info.get("format", {}).get("duration", 0))
        if duration <= 0:
            return None
        seek = duration * _PHASH_SEEK_FRACTION

        # ── Step 2: extract one 32×32 greyscale frame ────────────────────
        frame_cmd = [
            ffmpeg_path(), "-ss", str(seek), "-i", str(file_path),
            "-vframes", "1",
            "-vf", _PHASH_FRAME_VF,
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

        # ── Step 3: DCT-based pHash (CPU-bound — run off the event loop) ──
        # The frame is exactly 32×32 greyscale bytes; the hash uses only the
        # 8×8 low-frequency block.  Offloaded to a worker thread so it never
        # blocks the loop (matching runs several files concurrently).
        return await asyncio.to_thread(_phash_from_gray_frame, raw)

    except Exception:
        return None


def _compute_frame_phash_sync(file_path: Path) -> Optional[str]:
    """
    Synchronous sibling of :func:`_compute_frame_phash` for the scan path.

    Scan builds file entries in a worker thread (``asyncio.to_thread`` in
    scan_stream) that cannot await the async version, so this mirrors it with
    blocking ``subprocess.run`` calls. It shares the exact frame filter
    (``_PHASH_FRAME_VF``), seek point (``_PHASH_SEEK_FRACTION``) and hash function
    (``_phash_from_gray_frame``) as the async path, so a scan-computed pHash is
    byte-for-byte the same value the fingerprint match would compute — no drift.

    Best-effort and never raises: a missing ffmpeg/ffprobe, an unreadable file, or
    a slow mount simply yields ``None`` and the pHash column stays empty for that
    file. Requires ffmpeg + ffprobe on PATH (identical assumption to the existing
    duration probe and thumbnail extraction on every build target).
    """
    try:
        probe = subprocess.run(
            [ffprobe_path(), "-v", "quiet", "-print_format", "json",
             "-show_entries", "format=duration", str(file_path)],
            capture_output=True, timeout=15,
        )
        if probe.returncode != 0:
            return None
        info = json.loads(probe.stdout or b"{}")
        duration = float(info.get("format", {}).get("duration", 0))
        if duration <= 0:
            return None
        seek = duration * _PHASH_SEEK_FRACTION

        ff = subprocess.run(
            [ffmpeg_path(), "-ss", str(seek), "-i", str(file_path),
             "-vframes", "1",
             "-vf", _PHASH_FRAME_VF,
             "-f", "rawvideo", "-pix_fmt", "gray",
             "pipe:1"],
            capture_output=True, timeout=30,
        )
        if ff.returncode != 0 or len(ff.stdout) < 1024:
            return None
        return _phash_from_gray_frame(ff.stdout)
    except Exception:
        return None


# ─── Client ────────────────────────────────────────────────────────────────────

def _classify_error(exc: Exception) -> str:
    """Map a lookup failure to a user-meaningful kind: auth | rate_limit | network.

    StashDB signals a bad key either as HTTP 401/403 or as a GraphQL
    "not authorized" error depending on the deployment — both classify as
    "auth". Raw exception text stays server-side (F15).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in (401, 403):
            return "auth"
        if code == 429:
            return "rate_limit"
        return "network"
    if isinstance(exc, RuntimeError) and "not authorized" in str(exc).lower():
        return "auth"
    return "network"


class StashDBClient:
    """
    Minimal async GraphQL client for StashDB.

    Authentication: pass the API key from your StashDB user profile as
    STASHDB_API_KEY environment variable.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("STASHDB_API_KEY")
        # Kind of the most recent lookup failure (auth/rate_limit/network),
        # None after a clean call. Reset on ENTRY to each lookup so a stale
        # flag can't mislabel a later success; cross-task attribution under
        # concurrency is harmless — the classified conditions are global (F15).
        self.last_error: Optional[str] = None
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
        self.last_error = None
        try:
            data = await self._query(_Q_SEARCH, {"term": term})
            return [self._parse(s) for s in data.get("searchScene", [])]
        except Exception as exc:
            self.last_error = _classify_error(exc)
            print(f"StashDB search error ({self.last_error}): {exc}")
            return []

    async def find_scene_by_id(self, scene_id: str) -> Optional[StashDBScene]:
        """Fetch a single scene by its StashDB UUID (e.g. from a scene URL).

        Returns the parsed scene, or None if it doesn't exist. Raises on a
        transport/GraphQL error so the caller can surface a clear message.
        """
        data = await self._query(_Q_FIND_SCENE, {"id": scene_id})
        scene = data.get("findScene")
        return self._parse(scene) if scene else None

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
        self.last_error = None
        try:
            # Outer list = one query group; inner list = fingerprints for that group
            data = await self._query(_Q_FINGERPRINTS, {"fingerprints": [fingerprints]})
            # Returns [[Scene, ...]] — one inner list per query group
            results_nested = data.get("findScenesBySceneFingerprints", [[]])
            flat = [scene for group in results_nested for scene in (group or [])]
            return [self._parse(s) for s in flat]
        except Exception as exc:
            self.last_error = _classify_error(exc)
            print(f"StashDB fingerprint error ({self.last_error}): {exc}")
            return []

    async def find_by_fingerprints_batch(
        self, groups: list[list[dict]]
    ) -> Optional[list[list[StashDBScene]]]:
        """
        Batched fingerprint lookup (F4) — one round-trip for many files.

        ``groups`` is one inner list of FingerprintQueryInput dicts per file
        (the shape find_by_fingerprint builds for a single file). Returns the
        parsed result lists positionally — ``result[i]`` belongs to
        ``groups[i]``, empty list on no hit — or **None on a transport/query
        error** so callers can fall back to the per-file path (which retries
        the fingerprint tier itself) instead of silently losing accuracy.
        """
        if not groups:
            return []
        self.last_error = None
        try:
            data = await self._query(_Q_FINGERPRINTS, {"fingerprints": groups})
            nested = data.get("findScenesBySceneFingerprints") or []
            out = [[self._parse(s) for s in (grp or [])] for grp in nested]
            # Positional contract: pad if the server returned fewer groups.
            while len(out) < len(groups):
                out.append([])
            return out
        except Exception as exc:
            self.last_error = _classify_error(exc)
            print(f"StashDB batch fingerprint error ({self.last_error}): {exc}")
            return None

    async def submit_fingerprint(
        self,
        scene_id: str,
        oshash: Optional[str] = None,
        phash: Optional[str] = None,
        duration: Optional[float] = None,
    ) -> bool:
        """Contribute this file's fingerprints for ``scene_id`` (F5, OPT-IN).

        Sends ONLY content hashes + duration — never file names, paths, or any
        other metadata. The stash-box schema requires a duration per
        fingerprint (Int!), so without one nothing is sent. Best-effort: one
        mutation per available hash, errors are classified into the server log
        and never raised, and ``last_error`` is deliberately NOT touched (these
        run in background tasks; polluting the shared classifier state could
        mislabel a concurrent lookup's failure). Returns True if at least one
        submission succeeded.
        """
        if not scene_id or duration is None:
            return False
        try:
            dur = int(round(float(duration)))
        except (TypeError, ValueError):
            return False
        if dur <= 0:
            return False
        ok = False
        for algorithm, value in (("OSHASH", oshash), ("PHASH", phash)):
            if not value:
                continue
            try:
                await self._query(_M_SUBMIT_FP, {"input": {
                    "scene_id": scene_id,
                    "fingerprint": {"hash": value, "algorithm": algorithm,
                                    "duration": dur},
                }})
                ok = True
            except Exception as exc:
                kind = _classify_error(exc)
                print(f"StashDB fingerprint submission error ({kind}): {exc}")
        return ok

    # ── parsing ────────────────────────────────────────────────────────

    @staticmethod
    def _parse(data: dict) -> StashDBScene:
        studio = data.get("studio") or {}
        site = studio.get("name") or ""
        parent = (studio.get("parent") or {}).get("name") or None

        performers = []
        performer_genders = []
        for a in data.get("performers", []):
            perf = a.get("performer", {})
            if not perf.get("name"):
                continue
            performers.append(perf["name"])
            gender = perf.get("gender")
            performer_genders.append(str(gender).strip().lower() if gender else None)
        # F6: keep the per-scene credit ("as" = alias credited in THIS scene)
        # alongside the canonical name — free alias-learning material.
        performer_credits = [
            {"name": a["performer"]["name"], "as": (a.get("as") or None)}
            for a in data.get("performers", [])
            if a.get("performer", {}).get("name")
        ]
        tags = [t["name"] for t in data.get("tags", []) if t.get("name")]
        urls = [u.get("url") for u in data.get("urls", []) if u.get("url")]

        # Pick best image: prefer widest; the runner-up becomes fanart (F7).
        images = sorted(data.get("images", []), key=lambda i: i.get("width", 0), reverse=True)
        poster_url = images[0]["url"] if images else None
        fanart_url = images[1]["url"] if len(images) >= 2 else None

        return StashDBScene(
            id=data.get("id", ""),
            title=data.get("title") or "",
            site=site or None,
            network=parent,
            performers=performers,
            performer_genders=performer_genders,
            release_date=data.get("date"),
            duration=data.get("duration"),
            tags=tags,
            poster_url=poster_url,
            description=data.get("details") or None,
            code=data.get("code") or None,
            director=data.get("director") or None,
            url=urls[0] if urls else None,
            performer_credits=performer_credits,
            fanart_url=fanart_url,
        )
