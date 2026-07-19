"""
Adult scene detector - extracts metadata from filenames.
Supports common adult content filename patterns.
"""

import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

from app.core.matcher import normalize


class MediaType(str, Enum):
    SCENE = "scene"
    UNKNOWN = "unknown"


@dataclass
class AdultDetectionResult:
    """Detection result from filename parsing."""
    media_type: MediaType
    clean_name: str
    site: Optional[str] = None
    performers: list[str] = field(default_factory=list)
    scene_title: Optional[str] = None
    release_date: Optional[str] = None  # YYYY-MM-DD
    year: Optional[int] = None
    quality: Optional[str] = None  # 720p, 1080p, 2160p, 4K
    source: Optional[str] = None  # WEB-DL, BluRay, DVDRip
    video_format: Optional[str] = None  # x264, x265, HEVC
    group: Optional[str] = None
    original_filename: str = ""
    # Derived signals (review item D4) — surfaced for matching/search:
    #   normalized_name: lowercased, separators→spaces, scene-release junk
    #                    (XXX/1080p/WEB-DL/x265/-GROUP…) stripped.
    #   tokens:          unique word tokens of normalized_name (cheap pre-filter).
    normalized_name: str = ""
    tokens: list[str] = field(default_factory=list)
    # F8: "folder" when site/title/date were (partly) inferred from an ancestor
    # directory name rather than the filename; None otherwise. The UI shows a
    # subtle hint so the user knows where the guess came from.
    context_source: Optional[str] = None


# Video file extensions — all formats the scanner will consider
VIDEO_EXTENSIONS = {
    # MPEG-4 / ISO Base Media
    ".mp4", ".m4v", ".m4b", ".m4p",
    # Matroska
    ".mkv", ".mk3d",
    # QuickTime / Apple
    ".mov", ".qt",
    # AVI / DivX / Xvid
    ".avi", ".divx", ".xvid",
    # Windows Media
    ".wmv", ".asf", ".wtv", ".dvr-ms",
    # Flash / FLV
    ".flv", ".f4v", ".f4p",
    # WebM
    ".webm",
    # Ogg / Theora
    ".ogv", ".ogm", ".ogg",
    # MPEG / Transport Streams
    ".mpg", ".mpeg", ".mpe", ".m1v", ".m2v",
    ".ts", ".m2ts", ".mts", ".trp", ".tp",
    # DVD
    ".vob", ".ifo",
    # RealMedia  (read-only — metadata embedded via NFO sidecar)
    ".rmvb", ".rm", ".ra", ".rv",
    # 3GPP / Mobile
    ".3gp", ".3g2", ".3gpp", ".3gpp2",
    # Digital Video / Camcorder
    ".dv", ".dif",
    # Nullsoft / other streaming  (read-only)
    ".nsv",
    # AMV (portable media players)
    ".amv",
    # HEVC / H.264 raw
    ".hevc", ".h264", ".h265",
    # Miscellaneous
    ".nut", ".mxf", ".roq", ".fli", ".flc",
}

SUBTITLE_EXTENSIONS = {".srt", ".sub", ".ass", ".ssa", ".idx", ".sup", ".vtt"}


# Adult scene filename patterns (priority order)
ADULT_PATTERNS = [
    # Pattern 1: Site.YY.MM.DD.Performer.Scene.Title.XXX.1080p.MP4-GROUP
    re.compile(
        r'^([A-Za-z0-9]+)\.(\d{2})\.(\d{2})\.(\d{2})\.([A-Z][a-z]+(?:\.[A-Z][a-z]+)*)\.(.+?)\.XXX\.(\d{3,4}p)',
        re.IGNORECASE
    ),
    
    # Pattern 2: [Site] Performer - Scene Title (YYYY-MM-DD)
    re.compile(
        r'^\[([^\]]+)\]\s*([^-]+?)\s*-\s*([^(]+?)\s*\((\d{4}-\d{2}-\d{2})\)',
        re.IGNORECASE
    ),
    
    # Pattern 3: Site_Performer_Scene_XXX_1080p_WEB-DL
    re.compile(
        r'^([A-Za-z0-9]+)_([A-Z][a-z]+(?:_[A-Z][a-z]+)*)_(.+?)_XXX_(\d{3,4}p)',
        re.IGNORECASE
    ),
    
    # Pattern 4: Site - Performer - Scene Title (YYYY-MM-DD)
    re.compile(
        r'^([^-]+?)\s*-\s*([^-]+?)\s*-\s*([^(]+?)\s*\((\d{4}-\d{2}-\d{2})\)',
        re.IGNORECASE
    ),
    
    # Pattern 5: YYYY.MM.DD.Site.Performer.Scene.Quality
    re.compile(
        r'^(\d{4})\.(\d{2})\.(\d{2})\.([A-Za-z0-9]+)\.([A-Z][a-z]+(?:\.[A-Z][a-z]+)*)\.(.+?)\.(\d{3,4}p)',
        re.IGNORECASE
    ),
    
    # Pattern 6: Performer.Scene.Title.XXX.1080p
    re.compile(
        r'^([A-Z][a-z]+(?:\.[A-Z][a-z]+)*)\.(.+?)\.XXX\.(\d{3,4}p)',
        re.IGNORECASE
    ),
    
    # Pattern 7: Site.Performer.Scene.Title
    re.compile(
        r'^([A-Za-z0-9]+)\.([A-Z][a-z]+(?:\.[A-Z][a-z]+)*)\.(.+?)(?:\.XXX)?\.(\d{3,4}p)?',
        re.IGNORECASE
    ),
]


# Quality patterns
QUALITY_PATTERN = re.compile(r'\b(720p|1080p|2160p|4K|UHD)\b', re.IGNORECASE)

# Source patterns
SOURCE_PATTERN = re.compile(r'\b(WEB-DL|WEBRip|BluRay|BRRip|DVDRip|HDTV)\b', re.IGNORECASE)

# Video format patterns
FORMAT_PATTERN = re.compile(r'\b(x264|x265|HEVC|H\.264|H\.265|AVC)\b', re.IGNORECASE)

# Release group pattern
GROUP_PATTERN = re.compile(r'-([A-Z0-9]+)$', re.IGNORECASE)

# Scene-release junk tokens — resolution / source / codec / audio / misc flags
# that are NOT part of the title. Stripped before building the normalized name
# and token set so search and similarity work on the meaningful words only.
SCENE_TAG_RE = re.compile(
    r'\b(?:'
    r'xxx|'
    r'480p|540p|576p|720p|1080p|1440p|2160p|4k|uhd|hdr|hd|sd|'
    r'web-?dl|web-?rip|webcap|bluray|blu-?ray|brrip|bdrip|dvdrip|hdtv|hdrip|'
    r'x ?264|x ?265|h ?264|h ?265|hevc|avc|xvid|divx|'
    r'aac|ac3|dts|mp3|flac|opus|2 ?0|5 ?1|'
    r'multi|internal|proper|repack|uncut|remux'
    r')\b',
    re.IGNORECASE,
)

# "Site - Title (YYYY-MM-DD)" / "Site - Title" — a very common naming style the
# pattern table doesn't cover. Consolidated here (from main._extract_site_title)
# so BOTH datasources get site/title/date from these files (review item D4).
_SITE_TITLE_RE = re.compile(r'^(?P<site>.+?)\s+[-–]\s+(?P<title>.+?)\s*$')
_DATE_SUFFIX_RE = re.compile(r'\s*\((\d{4})[-./](\d{2})[-./](\d{2})\)\s*$')


def normalize_filename(name: str) -> str:
    """Return a clean, comparable form of a filename.

    Separators → spaces, scene-release junk + trailing ``-GROUP`` stripped, then
    run through ``matcher.normalize`` (accents/case/punctuation) so the detector
    and the matcher share ONE normalization definition.
    """
    s = re.sub(r'[._]', ' ', name)
    s = SCENE_TAG_RE.sub(' ', s)
    s = re.sub(r'-[A-Za-z0-9]+\s*$', ' ', s)   # trailing release group
    return normalize(s)


def _try_site_title(filename: str):
    """Parse 'Site - Title (YYYY-MM-DD)' / 'Site - Title'.

    Returns ``(site, title, release_date|None, year|None)`` or ``None``.
    """
    text = filename.strip()
    release_date = None
    year = None
    dm = _DATE_SUFFIX_RE.search(text)
    if dm:
        y, mo, d = dm.groups()
        release_date = f"{y}-{mo}-{d}"
        year = int(y)
        text = _DATE_SUFFIX_RE.sub("", text).strip()

    m = _SITE_TITLE_RE.match(text)
    if not m:
        return None
    site = m.group("site").strip()
    title = clean_text(m.group("title"))
    if not site or not title:
        return None
    return site, title, release_date, year


def is_video_file(path: Path) -> bool:
    """Check if file is a video file."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def is_subtitle_file(path: Path) -> bool:
    """Check if file is a subtitle file."""
    return path.suffix.lower() in SUBTITLE_EXTENSIONS


def clean_text(text: str) -> str:
    """Clean extracted text - replace dots/underscores with spaces."""
    text = re.sub(r'[._]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def extract_performers(text: str) -> list[str]:
    """
    Extract performer names from text.
    Handles formats like "Jane.Doe" or "Jane_Doe_And_John_Smith"
    """
    # Split on common separators
    text = re.sub(r'[._]', ' ', text)
    text = re.sub(r'\s+and\s+', ',', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+&\s+', ',', text)
    
    performers = [p.strip() for p in text.split(',') if p.strip()]
    return performers if performers else [text.strip()]


def parse_date(date_str: str) -> tuple[Optional[str], Optional[int]]:
    """
    Parse date string and return (YYYY-MM-DD, year).
    Handles formats: YY.MM.DD, YYYY-MM-DD, YYYY.MM.DD
    """
    # Full date: YYYY-MM-DD or YYYY.MM.DD
    match = re.match(r'(\d{4})[-.](\d{2})[-.](\d{2})', date_str)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}", int(year)
    
    # Short date: YY.MM.DD
    match = re.match(r'(\d{2})[-.](\d{2})[-.](\d{2})', date_str)
    if match:
        yy, month, day = match.groups()
        # Assume 20xx for dates
        year = f"20{yy}"
        return f"{year}-{month}-{day}", int(year)
    
    return None, None


def _apply_folder_context(det: AdultDetectionResult, path: Path,
                          known_sites: Optional[set]) -> None:
    """F8: fill detection gaps from up to two ancestor directory names.

    Only runs when the filename yielded no site. Filename-derived fields are
    never overwritten: a "Site - Title (Date)"-shaped folder supplies the site,
    plus title/date only when the filename produced no structure at all (no
    performers, no date — i.e. the generic-fallback case where scene_title is
    just the cleaned stem); a folder whose normalized name is in the injected
    ``known_sites`` set supplies the site alone. Mutates ``det`` in place and
    stamps ``context_source = "folder"`` when anything was filled.
    """
    if det.site:
        return
    # "Unstructured" = the generic fallback ran: its scene_title is merely the
    # cleaned stem, so a folder-shaped title is strictly better information.
    unstructured = not det.performers and det.release_date is None
    candidates = []
    for parent in (path.parent, path.parent.parent):
        name = parent.name.strip() if parent is not None and parent.name else ""
        if name:
            candidates.append(name)
    for name in candidates[:2]:
        st = _try_site_title(name)
        if st:
            f_site, f_title, f_date, f_year = st
            det.site = f_site
            if unstructured and f_title:
                det.scene_title = f_title
            if det.release_date is None and f_date:
                det.release_date = f_date
                det.year = f_year
            det.context_source = "folder"
            return
        if known_sites and normalize(name) in known_sites:
            det.site = name
            det.context_source = "folder"
            return


def detect(path: Path, known_sites: Optional[set] = None) -> AdultDetectionResult:
    """
    Detect adult scene metadata from filename.

    Args:
        path: File path
        known_sites: optional set of NORMALIZED site names (injected by the
            caller — this module stays storage-free, mirroring matcher's
            alias_resolver pattern). Used only for folder-context gap-fill
            (F8) when the filename itself yields no site.

    Returns:
        AdultDetectionResult with extracted metadata
    """
    det = _detect_filename(path)
    if det.site is None:
        _apply_folder_context(det, path, known_sites)
    return det


def _detect_filename(path: Path) -> AdultDetectionResult:
    """Filename-only detection (the pre-F8 ``detect`` body, unchanged)."""
    filename = path.stem  # Remove extension
    original = filename
    
    # Try each pattern in priority order. Dispatch by the pattern's INDEX (each
    # has a known, fixed group layout) rather than guessing from group
    # shape/content — the old shape-guessing crashed on optional/None groups and
    # mis-handled several patterns (review item D4: "brittle detector").
    for idx, pattern in enumerate(ADULT_PATTERNS):
        match = pattern.match(filename)
        if not match:
            continue
        g = match.groups()
        try:
            if idx == 0:
                # Site.YY.MM.DD.Performer.Scene.XXX.Quality
                release_date, year = parse_date(f"{g[1]}.{g[2]}.{g[3]}")
                return _build_result(original, g[0], extract_performers(g[4]),
                                     clean_text(g[5]), release_date, year, g[6], filename)
            if idx in (1, 3):
                # [Site] Performer - Scene (YYYY-MM-DD)  /  Site - Performer - Scene (Date)
                release_date = g[3]
                year = int(release_date[:4]) if release_date else None
                return _build_result(original, g[0].strip(), extract_performers(g[1]),
                                     clean_text(g[2]), release_date, year, None, filename)
            if idx == 2:
                # Site_Performer_Scene_XXX_Quality
                return _build_result(original, g[0], extract_performers(g[1]),
                                     clean_text(g[2]), None, None, g[3], filename)
            if idx == 4:
                # YYYY.MM.DD.Site.Performer.Scene.Quality
                release_date, year = parse_date(f"{g[0]}.{g[1]}.{g[2]}")
                return _build_result(original, g[3], extract_performers(g[4]),
                                     clean_text(g[5]), release_date, year, g[6], filename)
            if idx == 5:
                # Performer.Scene.Title.XXX.Quality  (no site)
                return _build_result(original, None, extract_performers(g[0]),
                                     clean_text(g[1]), None, None, g[2], filename)
            if idx == 6:
                # Site.Performer.Scene[.XXX][.Quality]  (quality optional → may be None)
                quality = g[3] if len(g) > 3 else None
                return _build_result(original, g[0], extract_performers(g[1]),
                                     clean_text(g[2]), None, None, quality, filename)
        except (IndexError, ValueError, TypeError):
            # Malformed match for this layout — fall through to the next pattern.
            continue

    # "Site - Title (Date)" / "Site - Title" — consolidated here so both
    # datasources benefit (was an ad-hoc fallback in main.py).
    st = _try_site_title(filename)
    if st:
        site, scene_title, release_date, year = st
        return _build_result(
            original, site, [], scene_title, release_date, year, None, filename
        )

    # Fallback: couldn't parse with patterns, do basic extraction. Routed through
    # _build_result so source/format/group AND the derived fields are still
    # populated (previously they were left blank here).
    clean = clean_text(filename)

    year_match = re.search(r'\b(19|20)\d{2}\b', filename)
    year = int(year_match.group(0)) if year_match else None

    return _build_result(
        original, None, [], clean, None, year, None, filename
    )


def _build_result(
    original: str,
    site: Optional[str],
    performers: list[str],
    scene_title: Optional[str],
    release_date: Optional[str],
    year: Optional[int],
    quality: Optional[str],
    filename: str,
) -> AdultDetectionResult:
    """Build detection result with additional metadata extraction."""
    
    # Extract source
    source_match = SOURCE_PATTERN.search(filename)
    source = source_match.group(1) if source_match else None
    
    # Extract video format
    format_match = FORMAT_PATTERN.search(filename)
    video_format = format_match.group(1) if format_match else None
    
    # Extract release group
    group_match = GROUP_PATTERN.search(filename)
    group = group_match.group(1) if group_match else None
    
    # Extract quality if not provided
    if not quality:
        quality_match = QUALITY_PATTERN.search(filename)
        quality = quality_match.group(1) if quality_match else None
    
    clean_name = scene_title or clean_text(filename)

    # Derived signals (D4): a junk-stripped normalized name + unique token set,
    # computed once here so every detection path (patterns, site-title, fallback)
    # carries them consistently.
    normalized_name = normalize_filename(filename)
    tokens = list(dict.fromkeys(normalized_name.split()))

    return AdultDetectionResult(
        media_type=MediaType.SCENE,
        clean_name=clean_name,
        site=site,
        performers=performers,
        scene_title=scene_title,
        release_date=release_date,
        year=year,
        quality=quality,
        source=source,
        video_format=video_format,
        group=group,
        original_filename=original,
        normalized_name=normalized_name,
        tokens=tokens,
    )
