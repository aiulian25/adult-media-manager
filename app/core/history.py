"""
Rename history tracking with undo support.
"""

import os
import json
import threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional


# Actions that created or moved a file and can therefore be reverted:
#   "move"     → move the file back to its original location.
#   "copy"/"hardlink"/"symlink" → remove the created file/link (original intact).
# "test" (dry run) and "undo" (already a revert) are intentionally excluded.
REVERTIBLE_ACTIONS = frozenset({"move", "copy", "hardlink", "symlink"})


@dataclass
class HistoryEntry:
    """A rename history entry."""
    id: str
    timestamp: str
    old_path: str
    new_path: str
    action: str
    success: bool
    error: Optional[str] = None
    # Shared id stamped on a video and its companion sidecars renamed by the
    # same operation (F10), so Revert/Undo restores the whole set together.
    # Default None keeps pre-upgrade history.json loadable (absent key → None).
    group_id: Optional[str] = None


class RenameHistory:
    """
    Tracks rename operations for undo support.
    Persists history to JSON file.
    """
    
    def __init__(self, history_file: Path):
        """
        Initialize history tracker.
        
        Args:
            history_file: Path to history JSON file
        """
        self.history_file = history_file
        self.entries: list[HistoryEntry] = []
        self._lock = threading.Lock()  # Serialise concurrent batch writes
        # Bound history size so the (full-file) save never grows unbounded.
        # AMM_HISTORY_MAX=0 disables the cap.  Same default on every build target.
        try:
            self._max_entries = int(os.getenv("AMM_HISTORY_MAX", "10000"))
        except ValueError:
            self._max_entries = 10000
        if self._max_entries < 0:
            self._max_entries = 0
        self._load()
    
    def _load(self):
        """Load history from file."""
        if self.history_file.exists():
            try:
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.entries = [HistoryEntry(**entry) for entry in data]
            except Exception as e:
                print(f"Error loading history: {e}")
                self.entries = []
    
    def _save(self):
        """Save history to file atomically (must be called with self._lock held).

        Writes to a temp file then os.replace()s it into place so a crash mid-
        write can never leave a truncated/corrupt history.json (which would
        break undo).  Mirrors the atomic-write pattern used for settings.json.
        """
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            data = [asdict(entry) for entry in self.entries]
            tmp = Path(str(self.history_file) + ".tmp")
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.history_file)
        except Exception as e:
            print(f"Error saving history: {e}")

    def _trim_locked(self):
        """Bound history to the most recent _max_entries (lock must be held)."""
        cap = self._max_entries
        if cap and len(self.entries) > cap:
            self.entries = self.entries[-cap:]

    @staticmethod
    def _make_entry(
        old_path: Path,
        new_path: Path,
        action: str,
        success: bool,
        error: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> HistoryEntry:
        now = datetime.now()
        return HistoryEntry(
            id=now.isoformat(),
            timestamp=now.strftime("%Y-%m-%d %H:%M:%S"),
            old_path=str(old_path),
            new_path=str(new_path),
            action=action,
            success=success,
            error=error,
            group_id=group_id,
        )

    def add_entry(
        self,
        old_path: Path,
        new_path: Path,
        action: str,
        success: bool,
        error: Optional[str] = None,
    ):
        """
        Add a single history entry (one disk write).

        Args:
            old_path: Original file path
            new_path: New file path
            action: Rename action type
            success: Whether operation succeeded
            error: Error message if failed
        """
        entry = self._make_entry(old_path, new_path, action, success, error)
        with self._lock:
            self.entries.append(entry)
            self._trim_locked()
            self._save()

    def add_entries(self, items: list[tuple]):
        """
        Append many entries with a SINGLE disk write.

        Each item is a tuple
        ``(old_path, new_path, action, success[, error[, group_id]])``.
        Used by the batch rename endpoint so an N-file batch performs one save
        instead of N full-file rewrites (previously O(n²) per batch).
        """
        if not items:
            return
        new_entries = [self._make_entry(*item) for item in items]
        with self._lock:
            self.entries.extend(new_entries)
            self._trim_locked()
            self._save()
    
    def get_recent(self, limit: int = 50) -> list[HistoryEntry]:
        """
        Get recent history entries.
        
        Args:
            limit: Maximum number of entries
            
        Returns:
            List of recent entries (newest first)
        """
        return list(reversed(self.entries[-limit:]))
    
    def get_entry(self, entry_id: str) -> Optional[HistoryEntry]:
        """Return the entry with the given id, or None if not found."""
        for entry in self.entries:
            if entry.id == entry_id:
                return entry
        return None

    def is_revertible(self, entry: HistoryEntry) -> bool:
        """Whether this entry's action can be reverted at all (action + success)."""
        return bool(entry.success) and entry.action in REVERTIBLE_ACTIONS

    def revert_entry(self, entry: HistoryEntry, is_allowed=None) -> tuple[bool, str]:
        """
        Revert a single rename / copy / link operation.

        Returns ``(success, code)`` where code is one of:
          ``ok`` · ``not_revertible`` · ``already_reverted`` · ``source_exists``
          · ``forbidden`` · ``error``.
        On success an ``"undo"`` entry is appended so the action is itself logged.

        ``is_allowed``: optional ``Callable[[Path], bool]`` used to confine the
        move/delete to permitted roots.  It is injected by the endpoint layer so
        this core class stays decoupled from the Docker/native path allowlist —
        no platform-specific logic lives here, so behaviour is identical on every
        build target.
        """
        action = entry.action
        old_path = Path(entry.old_path)
        new_path = Path(entry.new_path)

        if not entry.success or action not in REVERTIBLE_ACTIONS:
            return False, "not_revertible"

        # Security: never move or delete anything outside the allowed roots.
        if is_allowed is not None and not (is_allowed(new_path) and is_allowed(old_path)):
            return False, "forbidden"

        try:
            if action == "move":
                # Undo = move the file back to where it came from.
                if not new_path.exists():
                    return False, "already_reverted"
                if old_path.exists():
                    return False, "source_exists"
                old_path.parent.mkdir(parents=True, exist_ok=True)
                new_path.rename(old_path)
            else:
                # copy / hardlink / symlink → remove only the created file/link;
                # the original (old_path) is left untouched. is_symlink() catches
                # a broken symlink that exists() would report as missing.
                if not (new_path.exists() or new_path.is_symlink()):
                    return False, "already_reverted"
                new_path.unlink()
        except Exception as e:
            print(f"Error reverting {action} {new_path} -> {old_path}: {e}")
            return False, "error"

        self.add_entry(new_path, old_path, "undo", True)
        return True, "ok"

    def revert_group(self, group_id: str, is_allowed=None) -> list[tuple[str, bool, str]]:
        """Revert every successful, revertible entry sharing ``group_id`` (F10).

        Members are processed in append order — the video first, then its
        companions (that is the order rename_files logs them). Each member goes
        through :meth:`revert_entry`, so per-member action semantics, root
        confinement and the "undo" audit rows are identical to a single revert.

        Returns ``[(new_path, ok, code), ...]`` — one outcome per member.
        """
        if not group_id:
            return []
        members = [
            e for e in list(self.entries)
            if e.group_id == group_id
            and e.success and e.action in REVERTIBLE_ACTIONS
        ]
        results: list[tuple[str, bool, str]] = []
        for e in members:
            ok, code = self.revert_entry(e, is_allowed=is_allowed)
            results.append((e.new_path, ok, code))
        return results

    def undo_last(self, is_allowed=None) -> tuple[Optional[HistoryEntry], Optional[list]]:
        """
        Undo the most recent *move* that can still be reverted.

        Kept move-only for backward-compatible "Undo Last" semantics; per-entry
        :meth:`revert_entry` handles copy/hardlink/symlink. When the found move
        belongs to a rename group (F10), the WHOLE group is reverted — video and
        companion sidecars together (this also covers the case where the newest
        move row is a companion, not the video).

        Returns ``(entry, group_results)``: the undone entry (or None when
        nothing could be undone) and the per-file outcome list for grouped
        renames (None for ungrouped legacy entries).
        """
        for entry in reversed(self.entries):
            if entry.success and entry.action == "move":
                if entry.group_id:
                    results = self.revert_group(entry.group_id, is_allowed=is_allowed)
                    if any(ok for _, ok, _ in results):
                        return entry, results
                    codes = {code for _, ok, code in results if not ok}
                    # Fully-reverted/blocked groups don't stop the search —
                    # mirror the single-entry semantics below.
                    if codes and codes <= {"already_reverted", "source_exists"}:
                        continue
                    return None, None
                ok, code = self.revert_entry(entry, is_allowed=is_allowed)
                if ok:
                    return entry, None
                # A move that's already reverted or blocked by an existing source
                # shouldn't stop the search — keep looking further back (original
                # behaviour). forbidden/error are hard stops.
                if code in ("already_reverted", "source_exists"):
                    continue
                return None, None
        return None, None
    
    def clear(self):
        """Clear all history."""
        with self._lock:
            self.entries = []
            self._save()


# Global history instance (initialized in main.py)
history: Optional[RenameHistory] = None
