"""
Pluggable metadata-embedding strategy (review item R4).

This module holds the *portable, decision* part of metadata writing — which has
no dependency on FastAPI, the filesystem layout, or platform tool locations:

  • the container classification (which formats support fast in-place tagging),
  • the field→tag mapping shared by every writer (so embedded metadata is
    consistent no matter which tool wrote it),
  • and `plan_embed()` — the strategy planner that, given a container and the
    requested mode plus which in-place tools are actually available, returns the
    ordered list of strategies to attempt.

The heavy I/O executors (FFmpeg remux, mkvpropedit, AtomicParsley) stay in the
app layer because they need the staging directory and the platform-resolved tool
paths — those are the "thin platform adapters". Keeping the planner pure here
means it is unit-testable and there is ONE definition of the embedding policy
shared across Docker, deb and AppImage (no release drift).

Strategy vocabulary (canonical → what it does):
  • "remux"        – FFmpeg `-codec copy` rewrite (works for any writable
                     container; the universal last resort).
  • "mkvpropedit"  – Matroska in-place tag edit (near-instant, header-only).
  • "atomicparsley"– MP4/M4V/MOV in-place tag edit.
  • (nfo_only)     – no container write at all; the caller writes only the NFO.

Public mode names accepted by the API stay backward-compatible:
  "embed" ≡ remux-only (+ NFO), "smart" ≡ in-place-then-remux (+ NFO),
  "nfo_only" ≡ sidecar only (no container write),
  "embed_only" ≡ in-place-then-remux container write with NO NFO sidecar.

The container-write PLAN is the only thing decided here; whether an NFO sidecar
is ALSO written is decided by the app layer (per mode: nfo_only/smart/embed write
one; embed_only does not). "embed_only" therefore shares the fast in-place plan
with "smart" — the two differ only by the sidecar, which lives outside this pure
planner.
"""

from typing import Optional


# Containers that support fast in-place tagging (no media re-mux).
MATROSKA_EXTS: frozenset[str] = frozenset({".mkv", ".mk3d", ".webm"})
MP4_LIKE_EXTS: frozenset[str] = frozenset({".mp4", ".m4v", ".mov", ".m4a"})

# Public embed modes (the API/UI contract). Kept stable for backward compat.
#   nfo_only    → sidecar only (no container write)
#   embed_only  → container tags only, NO sidecar
#   smart/embed → both (container tags + sidecar)
EMBED_MODES: frozenset[str] = frozenset({"embed", "smart", "nfo_only", "embed_only"})


def validate_embed_mode(v: str) -> str:
    """Raise ValueError unless ``v`` is a recognised embed mode."""
    if v not in EMBED_MODES:
        raise ValueError(
            f"Invalid embed_mode: {v}. Must be one of: {sorted(EMBED_MODES)}"
        )
    return v


def ffmpeg_metadata_args(metadata: dict) -> list[str]:
    """
    Build the FFmpeg ``-metadata key=value`` argument list from a metadata dict.

    Shared field mapping (mirrored by the mkvpropedit/AtomicParsley writers):
        title       → title
        performers  → artist (comma-separated)
        site        → album
        release_date→ date
        tags        → comment (comma-separated)
    Empty values are omitted.
    """
    pairs = [
        ("title",   (metadata.get("title") or "").strip()),
        ("artist",  ", ".join(metadata.get("performers", []) or [])),
        ("album",   (metadata.get("site") or "")),
        ("date",    (metadata.get("release_date") or "")),
        ("comment", ", ".join(metadata.get("tags", []) or [])),
    ]
    args: list[str] = []
    for key, value in pairs:
        if value:
            args.extend(["-metadata", f"{key}={value}"])
    return args


def build_mkv_tags_xml(metadata: dict) -> Optional[str]:
    """
    Build a Matroska tags XML document (matroskatags.dtd) from metadata.

    Returns the serialised <Tags> element, or None when there are no tag values.
    Field mapping mirrors the FFmpeg path so embedded metadata is consistent:
        performers → ARTIST, site → ALBUM, release_date → DATE_RELEASED,
        tags → COMMENT, title → TITLE.
    Values are escaped by ElementTree, so API/user-supplied strings are safe.
    """
    import xml.etree.ElementTree as ET

    simple: list[tuple[str, str]] = []
    artist = ", ".join(metadata.get("performers", []) or [])
    if artist:
        simple.append(("ARTIST", artist))
    if metadata.get("site"):
        simple.append(("ALBUM", str(metadata["site"])))
    if metadata.get("release_date"):
        simple.append(("DATE_RELEASED", str(metadata["release_date"])))
    comment = ", ".join(metadata.get("tags", []) or [])
    if comment:
        simple.append(("COMMENT", comment))
    if metadata.get("title"):
        simple.append(("TITLE", str(metadata["title"])))

    if not simple:
        return None

    tags = ET.Element("Tags")
    tag = ET.SubElement(tags, "Tag")
    ET.SubElement(tag, "Targets")  # empty Targets = applies to the whole file
    for name, value in simple:
        s = ET.SubElement(tag, "Simple")
        ET.SubElement(s, "Name").text = name
        ET.SubElement(s, "String").text = value

    return ET.tostring(tags, encoding="unicode")


def plan_embed(
    ext: str,
    mode: str,
    *,
    has_mkvpropedit: bool = False,
    has_atomicparsley: bool = False,
) -> list[str]:
    """
    Return the ordered list of embedding strategies to attempt for one file.

    The list is tried in order until one succeeds; "remux" is always appended as
    the universal fallback after an in-place attempt, so the final on-disk result
    matches plain "embed" even when the in-place tool is missing or fails.

    Args:
        ext: lowercased file extension (e.g. ".mkv").
        mode: public mode — "nfo_only" | "embed" | "smart" | "embed_only"
              (aliases: "remux" ≡ embed, "inplace" ≡ smart). "embed_only" shares
              "smart"'s in-place container plan — the two differ only by whether
              the app also writes a sidecar, which is decided outside this planner.
        has_mkvpropedit / has_atomicparsley: whether each in-place tool resolved.

    Returns:
        e.g. ["mkvpropedit", "remux"], ["atomicparsley", "remux"], ["remux"], [].
    """
    ext = (ext or "").lower()

    if mode == "nfo_only":
        return []
    if mode in ("embed", "remux"):
        return ["remux"]

    # "smart" / "inplace" / "embed_only": prefer the fast in-place editor for the
    # container, then fall back to the remux.
    if ext in MATROSKA_EXTS and has_mkvpropedit:
        return ["mkvpropedit", "remux"]
    if ext in MP4_LIKE_EXTS and has_atomicparsley:
        return ["atomicparsley", "remux"]
    return ["remux"]
