"""
Similarity matching engine for adult content.
Scores matches between detected files and API metadata.
"""

import os
import re
import unicodedata
from dataclasses import dataclass, field
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


# Single-token performer names ("Mia", "Riley") are highly ambiguous — many
# performers share a first name — so a single token aligning with one token of a
# full name is only weak evidence and must not be credited like a full match
# (review item D5: over-crediting short/substring names). This caps such cases.
SINGLE_TOKEN_SCORE = 0.5
# One first OR last name aligning between two full names (e.g. "Mia Malkova" vs
# "Mia Khalifa") is similarly ambiguous and down-weighted from the old flat 0.7.
PARTIAL_NAME_SCORE = 0.5


def _initials_align(a: str, b: str) -> bool:
    """True when one token is a single-letter initial of the other (d ↔ doe)."""
    if a == b:
        return True
    if len(a) == 1 and b.startswith(a):
        return True
    if len(b) == 1 and a.startswith(b):
        return True
    return False


def _performer_pair_score(f_perf: str, a_perf: str) -> float:
    """
    Score a single filename-performer against a single API-performer (0.0–1.0).

    Token-aware (review item D5) instead of the old character-substring test,
    which over-credited short names: "mia" was a substring of "mia malkova",
    "mia khalifa", … (all 0.9), and "ana" a substring of "ariana". The rules:

    - Exact normalized match → 1.0.
    - Two full names (first+last each): require BOTH to align for a strong score;
      first matches with the surname an initial of the other → 0.9; only one of
      first/last aligning is ambiguous → PARTIAL_NAME_SCORE.
    - A single-token name aligning with one token of a full name is weak evidence
      → SINGLE_TOKEN_SCORE (no longer 0.9).
    - Otherwise a conservative fuzzy fallback.
    """
    if f_perf == a_perf:
        return 1.0

    f_parts = f_perf.split()
    a_parts = a_perf.split()
    if not f_parts or not a_parts:
        return 0.0

    f_multi = len(f_parts) > 1
    a_multi = len(a_parts) > 1

    if f_multi and a_multi:
        first_align = _initials_align(f_parts[0], a_parts[0])
        last_align = _initials_align(f_parts[-1], a_parts[-1])
        if f_parts[0] == a_parts[0] and f_parts[-1] == a_parts[-1]:
            return 1.0
        if first_align and last_align:
            # At least one side abbreviated (Jane Doe vs Jane D.) — strong, not exact.
            return 0.9
        if first_align or last_align:
            # Only one name component aligns — could be a different performer.
            return PARTIAL_NAME_SCORE
    else:
        # One side is a lone token; only credit a token-equality, and only weakly.
        single = f_parts if not f_multi else a_parts
        multi = a_parts if not f_multi else f_parts
        if single[0] in multi:
            return SINGLE_TOKEN_SCORE

    # Conservative fuzzy fallback for typos / spacing variants.
    similarity = name_similarity(f_perf, a_perf)
    return similarity if similarity > 0.85 else 0.0


def match_performers(
    file_performers: list[str],
    api_performers: list[str],
    alias_resolver=None,
) -> float:
    """
    Match performers with token-aware fuzzy matching.

    Handles:
    - Name variations (Jane Doe vs Jane D.)
    - Multiple performers (intersection score)
    - Order independence

    Short/ambiguous names (single tokens, single shared name component) are
    deliberately down-weighted so a lone "Mia" can't masquerade as a confident
    match for any "Mia ..." (review item D5).

    Args:
        file_performers: Performers from filename
        api_performers: Performers from API
        alias_resolver: optional ``name -> canonical name | None`` callable
            (F12). A filename performer whose resolved canonical name equals an
            API performer scores 1.0 for that pair — a learned alias is exact
            knowledge, not fuzziness. Injected so this module stays pure;
            None (the default) is byte-for-byte the previous behavior.

    Returns:
        Match score 0.0-1.0
    """
    if not file_performers or not api_performers:
        return 0.0

    # Normalize all performer names
    file_norm = [normalize(p) for p in file_performers]
    api_norm = [normalize(p) for p in api_performers]

    matches = 0.0
    total = max(len(file_norm), len(api_norm))

    for raw, f_perf in zip(file_performers, file_norm):
        # Learned alias (F12): exact hit on the canonical name → full score.
        if alias_resolver:
            resolved = alias_resolver(raw)
            if resolved and normalize(str(resolved)) in api_norm:
                matches += 1.0
                continue
        best_score = 0.0
        for a_perf in api_norm:
            best_score = max(best_score, _performer_pair_score(f_perf, a_perf))
            if best_score >= 1.0:
                break
        matches += best_score

    return matches / total if total > 0 else 0.0


def match_site(file_site: Optional[str], api_site: str, site_resolver=None) -> float:
    """
    Match site/studio names with alias and CamelCase handling.

    site_resolver (F17): optional ``name -> canonical site | None`` callable,
    injected by the caller — same pure-module pattern as match_performers'
    alias_resolver. It consults the learned site-alias table plus the known-
    sites store (name AND network spellings); this module never touches
    storage itself. The legacy four-entry abbreviation dict (bg/rk/ts/bang)
    retired into the seeded site_aliases.json defaults.
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

    # Learned / known site alias (F17): a resolver hit that lands exactly on
    # the API site is authoritative (a user confirm or a provider record put
    # it there). A hit that merely overlaps keeps the old abbreviation score.
    if site_resolver:
        resolved = site_resolver(file_site)
        if resolved:
            resolved_norm = normalize(resolved)
            if resolved_norm == api_norm:
                return 1.0
            if resolved_norm and (resolved_norm in api_norm or api_norm in resolved_norm):
                return 0.95

    # Fuzzy match on both raw and camel-split versions
    sim = max(
        name_similarity(file_site, api_site),
        name_similarity(file_camel, api_camel),
    )
    if sim > 0.7:
        return sim

    return 0.0


# Date matching tolerance (review item D6). The old window was generous and flat
# (±7d → 0.9, ±30d → 0.7), so two *different* scenes by the same performers in the
# same month could still score a date "match". Releases are usually dated to the
# day (or off by a day or two between sites), so the curve is now steep: an exact
# date is strong evidence, a couple of days is normal cross-site drift, a week is
# weak, and anything past the configurable hard cap contributes nothing.
# AMM_DATE_TOLERANCE_DAYS sets that outer cap (default 7 — tightened from 30);
# raise it for libraries whose filename dates are unreliable. Same default on
# every build target.
try:
    DATE_TOLERANCE_DAYS = int(os.getenv("AMM_DATE_TOLERANCE_DAYS", "7"))
except ValueError:
    DATE_TOLERANCE_DAYS = 7
if DATE_TOLERANCE_DAYS < 0:
    DATE_TOLERANCE_DAYS = 0


def match_release_date(
    file_date: Optional[str],
    api_date: Optional[str],
    tolerance_days: Optional[int] = None,
) -> float:
    """
    Match release dates with a steep, asymmetric tolerance curve (review item D6).

    - exact            → 1.0
    - within 2 days    → 0.9  (normal cross-site dating drift)
    - within 7 days    → 0.6  (weak — same week)
    - within tolerance → 0.3  (soft window only when tolerance > 7)
    - beyond tolerance → 0.0  (hard cap)

    Args:
        file_date: Date from filename (YYYY-MM-DD)
        api_date: Date from API (YYYY-MM-DD)
        tolerance_days: Outer hard cap in days; defaults to AMM_DATE_TOLERANCE_DAYS

    Returns:
        Match score 0.0-1.0
    """
    if not file_date or not api_date:
        return 0.0

    if tolerance_days is None:
        tolerance_days = DATE_TOLERANCE_DAYS

    try:
        file_dt = datetime.strptime(file_date[:10], "%Y-%m-%d")
        api_dt = datetime.strptime(api_date[:10], "%Y-%m-%d")

        diff = abs((file_dt - api_dt).days)

        # Hard cap first so a tighter tolerance always wins over the buckets below.
        if diff > tolerance_days:
            return 0.0
        if diff == 0:
            return 1.0
        if diff <= 2:
            return 0.9
        if diff <= 7:
            return 0.6
        # Only reachable when tolerance_days > 7.
        return 0.3

    except (ValueError, TypeError):
        return 0.0


# ── Duration matching ────────────────────────────────────────────────────────
# Runtime is one of the strongest, cheapest disambiguators for adult scenes:
# many share titles/performers but the duration is nearly unique. We treat it as
# a confirmation/penalty layered ON TOP of the base cascade (not a fixed weight),
# so files without a probed duration — or APIs that don't return one — score
# exactly as before (no regression). Tunable constants:
DURATION_TOLERANCE = 0.03          # ±3% counts as a perfect duration match
DURATION_BONUS = 0.12              # max additive boost when durations agree
DURATION_MISMATCH_FACTOR = 0.75   # multiplier applied when durations clearly differ

# Title-only fallback (review item D2). The cascade weights title at just 0.1,
# so a file with ONLY a title (no performers/site/date) can never clear the
# acceptance threshold even on a perfect title. When the cascade fails we fall
# back to direct title similarity so strong title matches still surface — for
# BOTH datasources (previously only StashDB had an ad-hoc copy of this).
TITLE_ONLY_THRESHOLD = 0.50        # minimum title similarity to accept a fallback match
TITLE_ONLY_SCALE = 0.80            # cap text-only confidence (perfect title → 0.80)


def match_duration(
    file_seconds: Optional[float],
    api_seconds: Optional[float],
    tolerance: float = DURATION_TOLERANCE,
) -> float:
    """
    Score how closely two durations agree (0.0–1.0).

    Returns 1.0 when within ``tolerance`` (fractional, relative to the longer of
    the two), decaying linearly to 0.0 by ~3× tolerance, and 0.0 when either
    value is missing or non-positive. Comparing the *fractional* difference makes
    the tolerance scale-invariant (a few seconds matters for a 5-min clip, not a
    2-hour movie).
    """
    try:
        f = float(file_seconds)
        a = float(api_seconds)
    except (TypeError, ValueError):
        return 0.0
    if f <= 0 or a <= 0:
        return 0.0

    longer = max(f, a)
    rel_diff = abs(f - a) / longer
    if rel_diff <= tolerance:
        return 1.0
    if rel_diff >= 3 * tolerance:
        return 0.0
    # Linear decay between tolerance (→1.0) and 3× tolerance (→0.0)
    return 1.0 - (rel_diff - tolerance) / (2 * tolerance)


# Cascade field weights (must sum to 1.0). Kept as data so the absolute score,
# the renormalized agreement, and the evidence-coverage list all derive from ONE
# definition (review item D7).
CASCADE_WEIGHTS = {
    "performers": 0.4,
    "site": 0.3,
    "date": 0.2,
    "title": 0.1,
}
_TOTAL_WEIGHT = sum(CASCADE_WEIGHTS.values())


@dataclass
class MatchScore:
    """
    Result of scoring one file against one API scene (review item D7).

    - ``agreement``: renormalized confidence among the evidence that was actually
      comparable on both sides — i.e. "agreement among available fields", not
      "fraction of all possible evidence". This is the honest percentage shown to
      the user and fixes the misleading low % a correct-but-sparse match used to
      get (root cause of D2/U4).
    - ``rank``: the absolute weighted score (Σ field·weight, + duration
      adjustment). Used ONLY for ranking/thresholding so that a richer match still
      outranks a thin one and the acceptance bar is unchanged — keeping
      false-positive behaviour identical to before this change.
    - ``coverage``: fraction of total cascade weight that was comparable (how much
      evidence existed), so the UI can say "92% · based on title + duration only".
    - ``fields``: the comparable field keys that contributed (for that UI note).
    """
    agreement: float
    rank: float
    coverage: float
    fields: list[str] = field(default_factory=list)


def score_match(file_data: dict, api_result: dict, alias_resolver=None,
                site_resolver=None) -> MatchScore:
    """
    Score a file against an API scene, returning both the absolute (rank) score
    and the renormalized agreement + evidence coverage (review item D7).

    A field counts as *comparable* only when both sides carry it (matching the
    presence checks the old absolute cascade used), so ``rank`` is bit-for-bit the
    same value the previous ``adult_cascade_score`` returned — no change to which
    candidate is picked or accepted.

    Args:
        file_data: Detected file metadata (may include ``duration_seconds``)
        api_result: API scene metadata (may include ``duration`` in seconds)
        alias_resolver: optional learned-alias lookup forwarded to
            :func:`match_performers` (F12); None = unchanged behavior
        site_resolver: optional learned-site lookup forwarded to
            :func:`match_site` (F17); None = unchanged behavior
    """
    weighted = 0.0        # Σ score·weight over comparable fields  → absolute base
    present_weight = 0.0  # Σ weight over comparable fields        → coverage
    fields: list[str] = []

    # 1. Performer matching (40% weight)
    file_performers = file_data.get("performers", [])
    api_performers = api_result.get("performers", [])
    if file_performers and api_performers:
        w = CASCADE_WEIGHTS["performers"]
        weighted += match_performers(file_performers, api_performers,
                                     alias_resolver=alias_resolver) * w
        present_weight += w
        fields.append("performers")

    # 2. Site/studio matching (30% weight)
    file_site = file_data.get("site")
    if file_site:
        w = CASCADE_WEIGHTS["site"]
        weighted += match_site(file_site, api_result.get("site", ""),
                               site_resolver=site_resolver) * w
        present_weight += w
        fields.append("site")

    # 3. Date matching (20% weight)
    file_date = file_data.get("release_date")
    api_date = api_result.get("release_date")
    if file_date and api_date:
        w = CASCADE_WEIGHTS["date"]
        weighted += match_release_date(file_date, api_date) * w
        present_weight += w
        fields.append("date")

    # 4. Title similarity (10% weight)
    file_title = file_data.get("scene_title") or file_data.get("clean_name", "")
    api_title = api_result.get("title", "")
    if file_title and api_title:
        w = CASCADE_WEIGHTS["title"]
        weighted += name_similarity(file_title, api_title) * w
        present_weight += w
        fields.append("title")

    # 5. Duration disambiguation (D1) — a confirmation/penalty applied AFTER the
    #    cascade, to BOTH the absolute rank and the renormalized agreement so the
    #    two stay consistent. No-op when either duration is missing.
    rank = _apply_duration_adjustment(weighted, file_data, api_result)
    agreement = weighted / present_weight if present_weight > 0 else 0.0
    agreement = _apply_duration_adjustment(agreement, file_data, api_result)
    if file_data.get("duration_seconds") and api_result.get("duration"):
        fields.append("duration")

    coverage = present_weight / _TOTAL_WEIGHT
    return MatchScore(agreement=agreement, rank=rank, coverage=coverage, fields=fields)


def adult_cascade_score(file_data: dict, api_result: dict) -> float:
    """
    Backward-compatible scalar score. Returns the renormalized **agreement**
    (the honest 0–1 confidence shown to users); callers that need ranking or the
    evidence coverage should use :func:`score_match` instead.
    """
    return score_match(file_data, api_result).agreement


def _apply_duration_adjustment(score: float, file_data: dict, api_result: dict) -> float:
    """
    Nudge a 0–1 match score by how well the durations agree (see match_duration).
    No-op when either duration is missing, so absent data never changes the score.
    Shared by the cascade and the title-only fallback so both treat runtime the
    same way.
    """
    file_dur = file_data.get("duration_seconds")
    api_dur = api_result.get("duration")
    if not (file_dur and api_dur):
        return score
    dur = match_duration(file_dur, api_dur)
    if dur >= 0.5:
        return min(1.0, score + DURATION_BONUS * dur)
    return score * DURATION_MISMATCH_FACTOR


def find_best_match(
    file_data: dict, api_results: list[dict], alias_resolver=None,
    site_resolver=None,
) -> Optional[tuple[dict, MatchScore]]:
    """
    Find best matching result from API results, returning the chosen scene and its
    :class:`MatchScore` (renormalized agreement + evidence coverage, review D7).

    Ranking and the acceptance threshold use the **absolute** ``rank`` score, so a
    richer match still wins and the bar is unchanged — only the *reported*
    confidence is the honest renormalized agreement. The minimum threshold adapts:
    rich metadata (performers/site/date) needs a higher bar; sparse metadata
    relies more on the search engine ranking and accepts a lower score.

    Title-only fallback (D2): the cascade weights title at only 0.1, so a file
    with just a title can't clear the threshold even on a perfect title. When the
    cascade fails, we fall back to direct title similarity (capped for ranking, and
    nudged by duration when available) so strong title matches still surface — for
    BOTH the TPDB and StashDB paths from one implementation.
    """
    if not api_results:
        return None

    best_match = None
    best: Optional[MatchScore] = None

    for result in api_results:
        ms = score_match(file_data, result, alias_resolver=alias_resolver,
                         site_resolver=site_resolver)
        if best is None or ms.rank > best.rank:
            best = ms
            best_match = result

    # Adaptive threshold: lower when no performers/site/date detected
    has_performers = bool(file_data.get("performers"))
    has_site       = bool(file_data.get("site"))
    has_date       = bool(file_data.get("release_date"))
    rich_metadata  = has_performers or has_site or has_date

    min_threshold = 0.25 if rich_metadata else 0.12

    if best is not None and best.rank >= min_threshold:
        return (best_match, best)

    # ── Title-only fallback ──────────────────────────────────────────────────
    # Rank surviving candidates by capped title similarity (duration as a
    # tie-breaker/penalty) and accept the strongest if it clears the title bar.
    # The reported agreement is the raw (duration-adjusted) title similarity —
    # honest for a single comparable field — with coverage = title weight only.
    file_title = file_data.get("scene_title") or file_data.get("clean_name", "")
    if file_title:
        fb_match = None
        fb: Optional[MatchScore] = None
        for result in api_results:
            sim = name_similarity(file_title, result.get("title", ""))
            if sim < TITLE_ONLY_THRESHOLD:
                continue
            rank = _apply_duration_adjustment(sim * TITLE_ONLY_SCALE, file_data, result)
            if fb is None or rank > fb.rank:
                agreement = _apply_duration_adjustment(sim, file_data, result)
                fields = ["title"]
                if file_data.get("duration_seconds") and result.get("duration"):
                    fields.append("duration")
                fb = MatchScore(
                    agreement=agreement,
                    rank=rank,
                    coverage=CASCADE_WEIGHTS["title"] / _TOTAL_WEIGHT,
                    fields=fields,
                )
                fb_match = result
        if fb_match is not None:
            return (fb_match, fb)

    return None
