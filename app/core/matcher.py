"""
Similarity matching engine for adult content.
Scores matches between detected files and API metadata.
"""

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional
from datetime import datetime, timedelta


def normalize(text: str) -> str:
    """
    Normalize text for comparison.
    Strips accents, lowercases, collapses whitespace.
    """
    if not text:
        return ""
    
    # Decompose unicode, strip combining marks (accents)
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = text.lower()
    
    # Normalize separators
    text = re.sub(r'[._\-–—:;!?,\'\"()[\]{}]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def tokenize(text: str) -> list[str]:
    """Split normalized text into word tokens."""
    return normalize(text).split()


def name_similarity(a: str, b: str) -> float:
    """
    String similarity using SequenceMatcher.
    
    Args:
        a: First string
        b: Second string
        
    Returns:
        Similarity score 0.0-1.0
    """
    if not a or not b:
        return 0.0
    
    a_norm = normalize(a)
    b_norm = normalize(b)
    
    if not a_norm or not b_norm:
        return 0.0
    
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def match_performers(file_performers: list[str], api_performers: list[str]) -> float:
    """
    Match performers with fuzzy matching.
    
    Handles:
    - Name variations (Jane Doe vs Jane D.)
    - Multiple performers (intersection score)
    - Order independence
    
    Args:
        file_performers: Performers from filename
        api_performers: Performers from API
        
    Returns:
        Match score 0.0-1.0
    """
    if not file_performers or not api_performers:
        return 0.0
    
    # Normalize all performer names
    file_norm = [normalize(p) for p in file_performers]
    api_norm = [normalize(p) for p in api_performers]
    
    matches = 0
    total = max(len(file_norm), len(api_norm))
    
    for f_perf in file_norm:
        best_score = 0.0
        for a_perf in api_norm:
            # Exact match
            if f_perf == a_perf:
                best_score = 1.0
                break
            
            # Check if one name is contained in the other
            if f_perf in a_perf or a_perf in f_perf:
                best_score = max(best_score, 0.9)
            
            # Check first/last name matches
            f_parts = f_perf.split()
            a_parts = a_perf.split()
            if f_parts and a_parts:
                # First name match
                if f_parts[0] == a_parts[0]:
                    best_score = max(best_score, 0.7)
                # Last name match
                if len(f_parts) > 1 and len(a_parts) > 1:
                    if f_parts[-1] == a_parts[-1]:
                        best_score = max(best_score, 0.7)
            
            # Fuzzy match
            similarity = name_similarity(f_perf, a_perf)
            if similarity > 0.8:
                best_score = max(best_score, similarity)
        
        matches += best_score
    
    return matches / total if total > 0 else 0.0


def match_site(file_site: Optional[str], api_site: str) -> float:
    """
    Match site/studio names with abbreviation and CamelCase handling.
    """
    if not file_site:
        return 0.0

    file_norm = normalize(file_site)
    api_norm = normalize(api_site)

    # Exact match
    if file_norm == api_norm:
        return 1.0

    # Substring match
    if file_norm in api_norm or api_norm in file_norm:
        return 0.9

    # CamelCase split: "SweetSinner" → "sweet sinner"
    import re as _re
    def _split_camel(s: str) -> str:
        return _re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', s).lower()

    file_camel = normalize(_split_camel(file_site))
    api_camel  = normalize(_split_camel(api_site))
    if file_camel == api_camel:
        return 1.0
    if file_camel in api_camel or api_camel in file_camel:
        return 0.9

    # Common abbreviations
    abbreviations = {
        'bg': 'brazzers',
        'rk': 'reality kings',
        'ts': 'teamskeet',
        'bang': 'bangbros',
    }

    file_abbr = abbreviations.get(file_norm, file_norm)
    if file_abbr == api_norm or file_abbr in api_norm:
        return 0.95

    # Fuzzy match on both raw and camel-split versions
    sim = max(
        name_similarity(file_site, api_site),
        name_similarity(file_camel, api_camel),
    )
    if sim > 0.7:
        return sim

    return 0.0


def match_release_date(
    file_date: Optional[str], 
    api_date: Optional[str], 
    tolerance_days: int = 30
) -> float:
    """
    Match release dates with tolerance window.
    
    Args:
        file_date: Date from filename (YYYY-MM-DD)
        api_date: Date from API (YYYY-MM-DD)
        tolerance_days: Days tolerance for match
        
    Returns:
        Match score 0.0-1.0
    """
    if not file_date or not api_date:
        return 0.0
    
    try:
        file_dt = datetime.strptime(file_date[:10], "%Y-%m-%d")
        api_dt = datetime.strptime(api_date[:10], "%Y-%m-%d")
        
        diff = abs((file_dt - api_dt).days)
        
        # Exact match
        if diff == 0:
            return 1.0
        
        # Within 7 days
        if diff <= 7:
            return 0.9
        
        # Within tolerance
        if diff <= tolerance_days:
            return 0.7
        
        # Beyond tolerance
        return 0.0
        
    except (ValueError, TypeError):
        return 0.0


def adult_cascade_score(file_data: dict, api_result: dict) -> float:
    """
    Multi-metric cascade scoring for adult content.
    
    Scoring weights:
    - Performer match: 40%
    - Site/studio match: 30%
    - Release date: 20%
    - Scene title similarity: 10%
    
    Args:
        file_data: Detected file metadata
        api_result: API scene metadata
        
    Returns:
        Weighted match score 0.0-1.0
    """
    scores = []
    
    # 1. Performer matching (40% weight)
    file_performers = file_data.get("performers", [])
    api_performers = api_result.get("performers", [])
    if file_performers and api_performers:
        performer_score = match_performers(file_performers, api_performers)
        scores.append(performer_score * 0.4)
    else:
        scores.append(0.0)
    
    # 2. Site/studio matching (30% weight)
    file_site = file_data.get("site")
    api_site = api_result.get("site", "")
    if file_site:
        site_score = match_site(file_site, api_site)
        scores.append(site_score * 0.3)
    else:
        scores.append(0.0)
    
    # 3. Date matching (20% weight)
    file_date = file_data.get("release_date")
    api_date = api_result.get("release_date")
    if file_date and api_date:
        date_score = match_release_date(file_date, api_date)
        scores.append(date_score * 0.2)
    else:
        scores.append(0.0)
    
    # 4. Title similarity (10% weight)
    file_title = file_data.get("scene_title") or file_data.get("clean_name", "")
    api_title = api_result.get("title", "")
    if file_title and api_title:
        title_score = name_similarity(file_title, api_title)
        scores.append(title_score * 0.1)
    else:
        scores.append(0.0)
    
    return sum(scores)


def find_best_match(file_data: dict, api_results: list[dict]) -> Optional[tuple[dict, float]]:
    """
    Find best matching result from API results.
    The minimum threshold adapts: when the file has rich metadata (performers,
    site, date) a higher bar is required; when metadata is sparse we rely more
    on the search engine ranking and accept a lower score.
    """
    if not api_results:
        return None

    best_match = None
    best_score = 0.0

    for result in api_results:
        score = adult_cascade_score(file_data, result)
        if score > best_score:
            best_score = score
            best_match = result

    # Adaptive threshold: lower when no performers/site/date detected
    has_performers = bool(file_data.get("performers"))
    has_site       = bool(file_data.get("site"))
    has_date       = bool(file_data.get("release_date"))
    rich_metadata  = has_performers or has_site or has_date

    min_threshold = 0.25 if rich_metadata else 0.12

    if best_score >= min_threshold:
        return (best_match, best_score)

    return None
