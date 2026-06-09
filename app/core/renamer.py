"""
File renamer - handles move, copy, hardlink, symlink operations.
"""

import shutil
from pathlib import Path
from enum import Enum
from dataclasses import dataclass
from typing import Optional


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
