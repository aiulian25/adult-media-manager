"""
Adult scene detector - extracts metadata from filenames.
Supports common adult content filename patterns.
"""

import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


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

SUBTITLE_EXTENSIONS = {".srt", ".sub", ".ass", ".ssa", ".idx", ".sup"}


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


def detect(path: Path) -> AdultDetectionResult:
    """
    Detect adult scene metadata from filename.
    
    Args:
        path: File path
        
    Returns:
        AdultDetectionResult with extracted metadata
    """
    filename = path.stem  # Remove extension
    original = filename
    
    # Try each pattern in priority order
    for pattern in ADULT_PATTERNS:
        match = pattern.match(filename)
        if match:
            groups = match.groups()
            
            # Pattern 1: Site.YY.MM.DD.Performer.Scene.XXX.Quality
            if len(groups) >= 7 and groups[1].isdigit() and groups[2].isdigit():
                site = groups[0]
                date_str = f"{groups[1]}.{groups[2]}.{groups[3]}"
                release_date, year = parse_date(date_str)
                performers = extract_performers(groups[4])
                scene_title = clean_text(groups[5])
                quality = groups[6] if len(groups) > 6 else None
                
                return _build_result(
                    original, site, performers, scene_title,
                    release_date, year, quality, filename
                )
            
            # Pattern 2: [Site] Performer - Scene (YYYY-MM-DD)
            elif len(groups) >= 4 and '-' in groups[3]:
                site = groups[0].strip()
                performers = extract_performers(groups[1])
                scene_title = clean_text(groups[2])
                release_date = groups[3]
                year = int(release_date[:4]) if release_date else None
                
                return _build_result(
                    original, site, performers, scene_title,
                    release_date, year, None, filename
                )
            
            # Pattern 3: Site_Performer_Scene_XXX_Quality
            elif len(groups) >= 4 and '_' in original:
                site = groups[0]
                performers = extract_performers(groups[1])
                scene_title = clean_text(groups[2])
                quality = groups[3]
                
                return _build_result(
                    original, site, performers, scene_title,
                    None, None, quality, filename
                )
            
            # Pattern 4: Site - Performer - Scene (Date)
            elif len(groups) >= 4:
                site = groups[0].strip()
                performers = extract_performers(groups[1])
                scene_title = clean_text(groups[2])
                release_date = groups[3]
                year = int(release_date[:4]) if release_date else None
                
                return _build_result(
                    original, site, performers, scene_title,
                    release_date, year, None, filename
                )
            
            # Pattern 5: YYYY.MM.DD.Site.Performer.Scene.Quality
            elif len(groups) >= 7 and groups[0].isdigit() and len(groups[0]) == 4:
                date_str = f"{groups[0]}.{groups[1]}.{groups[2]}"
                release_date, year = parse_date(date_str)
                site = groups[3]
                performers = extract_performers(groups[4])
                scene_title = clean_text(groups[5])
                quality = groups[6] if len(groups) > 6 else None
                
                return _build_result(
                    original, site, performers, scene_title,
                    release_date, year, quality, filename
                )
    
    # Fallback: couldn't parse with patterns, do basic extraction
    clean = clean_text(filename)
    
    # Extract quality
    quality_match = QUALITY_PATTERN.search(filename)
    quality = quality_match.group(1) if quality_match else None
    
    # Extract year
    year_match = re.search(r'\b(19|20)\d{2}\b', filename)
    year = int(year_match.group(0)) if year_match else None
    
    return AdultDetectionResult(
        media_type=MediaType.SCENE,
        clean_name=clean,
        site=None,
        performers=[],
        scene_title=clean,
        release_date=None,
        year=year,
        quality=quality,
        source=None,
        video_format=None,
        group=None,
        original_filename=original,
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
    )
