"""
Template formatter for adult content naming.
Provides template variables for flexible naming schemes.
"""

import re
from pathlib import Path
from typing import Optional, Any


# Canonical set of placeholder names apply_template can resolve (the union of
# the bindings from extract_template_vars and the derived keys computed below).
# This is the SINGLE SOURCE OF TRUTH for UI validation — it is surfaced via
# /api/templates and /api/preview-paths so the client never hardcodes a list
# that could silently drift from the formatter and produce false warnings.
TEMPLATE_VARS: frozenset[str] = frozenset({
    "site", "network", "performer", "performers", "scene", "title", "id",
    "clean_name", "date", "year", "month", "day", "quality", "vf", "source", "group",
    "duration",
})


# Default templates — date is embedded as file metadata, NOT in filename
TEMPLATES = {
    "site_date":        "{site}/{performer}/{site}.{scene}.{quality}",
    "performer_focus":  "{performer}/{site} - {scene}",
    "studio_organized": "{site}/{year}/{scene}",
    "simple":           "{performer} - {scene} ({site})",
    "multi_performer":  "{site}/{performers}/{scene}",
    "dated_folders":    "{site}/{year}/{month}/{performer}.{scene}",
}


def _pad(value: Any, width: int = 2) -> str:
    """Zero-pad a number."""
    if value is None:
        return ""
    try:
        return str(int(value)).zfill(width)
    except (ValueError, TypeError):
        return str(value)


def apply_template(template: str, bindings: dict[str, Any]) -> str:
    """
    Apply a naming template with {variable} placeholders.
    
    Supported variables:
      {site}        - Site/studio name
      {network}     - Parent network
      {performer}   - Primary performer (first in list)
      {performers}  - All performers (comma-separated)
      {scene}       - Scene title
      {title}       - Alias for {scene}
      {id}          - Scene ID from TPDB
      {date}        - Release date (YYYY-MM-DD)
      {year}        - Year only (YYYY)
      {month}       - Month only (MM)
      {day}         - Day only (DD)
      {quality}     - Resolution (1080p, 4K, etc.)
      {vf}          - Video format (x264, x265)
      {source}      - Source (WEB-DL, BluRay)
      {group}       - Release group
    
    Args:
        template: Template string with {var} placeholders
        bindings: Dictionary of template variables
        
    Returns:
        Formatted string
    """
    # Pre-compute derived bindings
    date = bindings.get("date", "")
    performers_list = bindings.get("performers", [])
    
    # Date components
    year = ""
    month = ""
    day = ""
    if date and len(date) >= 10:
        parts = date.split("-")
        if len(parts) >= 3:
            year = parts[0]
            month = parts[1]
            day = parts[2]
    elif bindings.get("year"):
        year = str(bindings.get("year"))
    
    # Performers
    performer = performers_list[0] if performers_list else ""
    performers_str = ", ".join(performers_list) if performers_list else ""

    # Duration → "NNmin" (whole minutes, floored). Read from raw seconds under
    # either "duration_seconds" (what a scanned file_data carries) or "duration"
    # (what extract_template_vars binds). Empty string when absent/zero/invalid,
    # so {duration} simply renders to nothing rather than crashing.
    _dur_raw = bindings.get("duration_seconds")
    if _dur_raw is None:
        _dur_raw = bindings.get("duration")
    duration_str = ""
    try:
        _secs = float(_dur_raw)
        if _secs > 0:
            duration_str = f"{int(_secs // 60)}min"
    except (TypeError, ValueError):
        duration_str = ""

    derived = {
        "year": year,
        "month": month,
        "day": day,
        "performer": performer,
        "performers": performers_str,
        "title": bindings.get("scene", ""),  # Alias for scene
        "duration": duration_str,
    }
    
    all_bindings = {**bindings, **derived}
    
    def replacer(m: re.Match) -> str:
        key = m.group(1)
        val = all_bindings.get(key)
        if val is None or val == "":
            return ""
        return str(val)

    result = re.sub(r'\{(\w+)\}', replacer, template)

    # §4.5 — If {scene}/{title} resolved to empty, substitute {id} (TPDB scene
    # ID) if available, otherwise {clean_name} from the detector.  This ensures
    # every output path has at least one unique, non-empty filename component.
    if not all_bindings.get("scene"):
        _id_raw = all_bindings.get("id")
        _id_str = str(_id_raw).strip() if _id_raw is not None else ""
        fallback = _id_str or str(all_bindings.get("clean_name") or "").strip()
        if fallback:
            # Inline the fallback directly for {scene} and its alias {title},
            # then re-run replacer for all remaining placeholders.
            tpl_with_fallback = re.sub(r'\{scene\}|\{title\}', re.escape(fallback), template)
            result = re.sub(r'\{(\w+)\}', replacer, tpl_with_fallback)

    # Clean up artifacts from empty bindings
    result = re.sub(r'  +', ' ', result)  # Multiple spaces
    result = re.sub(r'\s*-\s*-\s*', ' - ', result)  # Double dashes
    result = re.sub(r'/+', '/', result)  # Multiple slashes
    result = re.sub(r'\s*\(\s*\)', '', result)  # Empty parens
    result = re.sub(r'\s*\[\s*\]', '', result)  # Empty brackets
    result = result.strip(' -/')
    
    return result


def sanitize_filename(name: str) -> str:
    """
    Remove characters not allowed in filenames.
    
    Args:
        name: Filename or path component
        
    Returns:
        Sanitized string
    """
    # Replace problematic characters
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    
    # Replace control characters
    name = re.sub(r'[\x00-\x1f]', '', name)
    
    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name)
    
    # Trim dots and spaces from ends (Windows compat)
    name = name.strip('. ')
    
    return name


def build_new_path(
    original_path: Path,
    template: str,
    bindings: dict[str, Any],
    output_dir: Optional[Path] = None,
) -> Path:
    """
    Build new file path from template.
    
    Args:
        original_path: Original file path
        template: Naming template
        bindings: Template variables
        output_dir: Output directory (default: same as original)
        
    Returns:
        New file path
    """
    # Apply template
    new_name = apply_template(template, bindings)

    # Sanitize each path component
    parts = new_name.split("/")
    parts = [sanitize_filename(p) for p in parts if p]

    # Add file extension from original
    extension = original_path.suffix

    # Guard: if all template parts resolved to empty (every binding was blank),
    # fall back to a deterministic name so files never collide with each other
    # or with their source path.
    if not parts:
        import uuid as _uuid
        parts = [f"unmatched_{_uuid.uuid4().hex[:8]}"]

    parts[-1] = parts[-1] + extension

    # Build path
    if output_dir:
        base = output_dir
    else:
        base = original_path.parent

    new_path = base
    for part in parts:
        new_path = new_path / part

    return new_path


def _strip_performer_prefix(title: str, performers: list) -> str:
    """
    Strip performer names from the start of TPDB scene titles.

    TPDB commonly formats scene titles as "Performer Name, Actual Title"
    (e.g. "Sladyen Skaya, Anal Debut").  This strips that prefix so
    {scene} only contains the actual scene title ("Anal Debut").

    Returns the original title unchanged when the stripped result would be
    empty (e.g. TPDB returned only "Performer Name, ").
    """
    if not title or not performers:
        return title
    for performer in performers:
        if not performer:
            continue
        # Match "Performer, Title" or "Performer And Performer2, Title"
        prefix = str(performer) + ", "
        if title.lower().startswith(prefix.lower()):
            stripped = title[len(prefix):].strip()
            return stripped if stripped else title
    return title


def extract_template_vars(scene_data: dict, file_data: dict) -> dict[str, Any]:
    """
    Extract template variables from scene and file data.
    
    Args:
        scene_data: Metadata from TPDB
        file_data: Detected file metadata
        
    Returns:
        Dictionary of template variables
    """
    performers = scene_data.get("performers") or file_data.get("performers", [])
    raw_title = scene_data.get("title") or file_data.get("scene_title", "")
    scene_title = _strip_performer_prefix(raw_title, performers)

    return {
        "site": scene_data.get("site") or file_data.get("site", ""),
        "network": scene_data.get("network", ""),
        "performers": performers,
        "scene": scene_title,
        "id": scene_data.get("id", ""),
        "clean_name": file_data.get("clean_name", ""),
        "date": scene_data.get("release_date") or file_data.get("release_date", ""),
        "year": None,  # Will be derived from date
        "month": None,  # Will be derived from date
        "day": None,  # Will be derived from date
        # Prefer the file's detected resolution; fall back to the scene's quality
        # (e.g. a manual/confirmed match that carries the user's chosen quality)
        # so {quality} still resolves when the filename had no resolution token.
        # Mirrors the scene→file fallback already used for site/date above.
        "quality": file_data.get("quality") or scene_data.get("quality", ""),
        "vf": file_data.get("video_format", ""),
        "source": file_data.get("source", ""),
        "group": file_data.get("group", ""),
        # Raw runtime seconds (from the scan's ffprobe / API); apply_template
        # renders {duration} from this as "NNmin". Falls back to the API scene's
        # duration when the file wasn't probed at scan time.
        "duration": file_data.get("duration_seconds") or scene_data.get("duration"),
    }
