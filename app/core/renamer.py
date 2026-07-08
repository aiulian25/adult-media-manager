"""
File renamer - handles move, copy, hardlink, symlink operations.
"""

import shutil
from pathlib import Path
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

from app.core.detector import SUBTITLE_EXTENSIONS


# Non-subtitle sidecar files that belong to a video and should travel with it on
# a rename: artwork (posters/fanart) and a pre-existing NFO. Combined with
# SUBTITLE_EXTENSIONS to form the full companion set.
COMPANION_ART_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".nfo"})
_COMPANION_EXTS: frozenset[str] = frozenset(SUBTITLE_EXTENSIONS) | COMPANION_ART_EXTS


class RenameAction(str, Enum):
    """Rename action types."""
    TEST = "test"  # Dry run, no actual changes
    MOVE = "move"  # Move/rename file
    COPY = "copy"  # Copy file
    HARDLINK = "hardlink"  # Create hard link
    SYMLINK = "symlink"  # Create symbolic link


@dataclass
class RenameResult:
    """Result of a rename operation."""
    success: bool
    old_path: Path
    new_path: Optional[Path]
    action: RenameAction
    error: Optional[str] = None
    # Results of any companion (subtitle/NFO/artwork) files moved alongside this
    # video with the same action. Empty for non-video renames or when there are
    # no companions. Populated by execute_rename_with_companions.
    companions: list["RenameResult"] = field(default_factory=list)


def _copy_file(src: str, dst: str) -> None:
    """Copy file data only, ignoring metadata/permission errors (NAS-safe)."""
    try:
        shutil.copy2(src, dst)
    except OSError:
        try:
            # copy2 failed (e.g. NAS rejects copystat/chmod) — try copy which
            # skips timestamps but still sets permissions.
            shutil.copy(src, dst)
        except OSError:
            # copy also failed (NAS rejects chmod) — use copyfile which
            # transfers only raw file data with no metadata operations at all.
            shutil.copyfile(src, dst)


def execute_rename(
    old_path: Path,
    new_path: Path,
    action: RenameAction,
) -> RenameResult:
    """
    Execute a rename operation.
    
    Args:
        old_path: Original file path
        new_path: New file path
        action: Type of operation
        
    Returns:
        RenameResult with operation status
    """
    # Validate old path exists
    if not old_path.exists():
        return RenameResult(
            success=False,
            old_path=old_path,
            new_path=None,
            action=action,
            error=f"Source file does not exist: {old_path}"
        )

    # Guard: source and destination are the same file — the file is already
    # at the correct destination.  Treat as a silent no-op success rather than
    # an error so that partially-organized libraries don't block the whole batch.
    if old_path.resolve() == new_path.resolve():
        return RenameResult(
            success=True,
            old_path=old_path,
            new_path=new_path,
            action=action,
        )

    # Test mode - don't actually perform operation
    if action == RenameAction.TEST:
        return RenameResult(
            success=True,
            old_path=old_path,
            new_path=new_path,
            action=action,
        )
    
    try:
        # Create parent directory if it doesn't exist
        new_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Check if target already exists
        if new_path.exists():
            return RenameResult(
                success=False,
                old_path=old_path,
                new_path=new_path,
                action=action,
                error=f"Target file already exists: {new_path}"
            )
        
        # Guard: destination must not already be a directory (shutil.move has
        # directory semantics and would silently keep the original filename).
        if new_path.is_dir():
            return RenameResult(
                success=False,
                old_path=old_path,
                new_path=new_path,
                action=action,
                error=f"Destination path is an existing directory: {new_path}",
            )

        # Perform the operation
        if action == RenameAction.MOVE:
            # Use Path.rename (os.rename) which always renames to the exact
            # destination path without directory semantics.  Fall back to
            # copy-then-delete when the source and destination are on
            # different filesystems (OSError / EXDEV).
            try:
                old_path.rename(new_path)
            except OSError:
                _copy_file(str(old_path), str(new_path))
                old_path.unlink()

        elif action == RenameAction.COPY:
            _copy_file(str(old_path), str(new_path))
        
        elif action == RenameAction.HARDLINK:
            new_path.hardlink_to(old_path)
        
        elif action == RenameAction.SYMLINK:
            new_path.symlink_to(old_path.resolve())
        
        else:
            return RenameResult(
                success=False,
                old_path=old_path,
                new_path=new_path,
                action=action,
                error=f"Unknown action: {action}"
            )
        
        return RenameResult(
            success=True,
            old_path=old_path,
            new_path=new_path,
            action=action,
        )
        
    except Exception as e:
        return RenameResult(
            success=False,
            old_path=old_path,
            new_path=new_path,
            action=action,
            error=str(e)
        )


def find_companions(video_path: Path) -> list[Path]:
    """Return sidecar files in the same directory that belong to ``video_path``.

    A companion is a subtitle / NFO / artwork file whose name is:
      • exactly ``<stem><suffix>``      → ``Scene.srt``, ``Scene.nfo``, ``Scene.jpg``
      • ``<stem>.<...>``                 → ``Scene.eng.srt`` (multi-part sub lang)
      • ``<stem>-<...>``                 → ``Scene-poster.jpg`` / ``Scene-fanart.jpg``

    The match is deliberately anchored to ``stem`` followed by ``.`` or ``-`` (not a
    bare prefix), so ``Scene2.srt`` is NOT treated as a companion of ``Scene.mp4``.
    The video itself and any non-companion extension (including sibling *videos*
    that happen to share the stem) are excluded. Best-effort: an unreadable
    directory yields an empty list rather than raising.
    """
    stem = video_path.stem
    if not stem:
        return []
    try:
        siblings = list(video_path.parent.iterdir())
    except OSError:
        return []

    out: list[Path] = []
    for p in siblings:
        if p == video_path:
            continue
        if p.suffix.lower() not in _COMPANION_EXTS:
            continue
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        name = p.name
        if name == stem + p.suffix or name.startswith(stem + ".") or name.startswith(stem + "-"):
            out.append(p)
    return out


def execute_rename_with_companions(
    old_path: Path,
    new_path: Path,
    action: RenameAction,
) -> RenameResult:
    """Rename a video AND co-locate its companion sidecars with the same action.

    Runs :func:`execute_rename` for the video, then for each companion moves it to
    the video's new stem while PRESERVING everything after the old stem — so
    ``Scene.eng.srt`` becomes ``NewName.eng.srt`` (keeping the ``.eng`` language
    tag) and ``Scene-poster.jpg`` becomes ``NewName-poster.jpg``. (Note: this is
    why we don't use ``new.stem + companion.suffix`` — ``Path.suffix`` of
    ``Scene.eng.srt`` is only ``.srt`` and would drop ``.eng``.)

    Companions are only chased when the primary actually relocated the file (not a
    TEST dry-run, a no-op self-rename, or a failure). A companion failure (e.g. its
    target already exists) is recorded on ``primary.companions`` but never fails the
    primary. Returns the primary result with companion results attached.
    """
    primary = execute_rename(old_path, new_path, action)

    if (
        not primary.success
        or action == RenameAction.TEST
        or old_path.resolve() == new_path.resolve()
    ):
        return primary

    old_stem = old_path.stem
    new_stem = new_path.stem
    for comp in find_companions(old_path):
        # comp.name is guaranteed to start with old_stem (see find_companions), so
        # the remainder is the part to keep verbatim after swapping the stem.
        remainder = comp.name[len(old_stem):]
        comp_new = new_path.with_name(new_stem + remainder)
        primary.companions.append(execute_rename(comp, comp_new, action))

    return primary


def batch_execute(operations: list[tuple[Path, Path, RenameAction]]) -> list[RenameResult]:
    """
    Execute multiple rename operations.
    
    Args:
        operations: List of (old_path, new_path, action) tuples
        
    Returns:
        List of RenameResult
    """
    results = []
    for old_path, new_path, action in operations:
        result = execute_rename(old_path, new_path, action)
        results.append(result)
    return results
