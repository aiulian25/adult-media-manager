"""
Rename history tracking with undo support.
"""

import json
import threading
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
from typing import Optional


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
        """Save history to file (must be called with self._lock held)."""
        try:
            self.history_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, 'w', encoding='utf-8') as f:
                data = [asdict(entry) for entry in self.entries]
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving history: {e}")
    
    def add_entry(
        self,
        old_path: Path,
        new_path: Path,
        action: str,
        success: bool,
        error: Optional[str] = None,
    ):
        """
        Add a history entry.
        
        Args:
            old_path: Original file path
            new_path: New file path
            action: Rename action type
            success: Whether operation succeeded
            error: Error message if failed
        """
        entry = HistoryEntry(
            id=datetime.now().isoformat(),
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            old_path=str(old_path),
            new_path=str(new_path),
            action=action,
            success=success,
            error=error,
        )
        with self._lock:
            self.entries.append(entry)
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
    
    def undo_last(self) -> Optional[HistoryEntry]:
        """
        Undo the last successful rename.
        
        Returns:
            The undone entry, or None if no entries to undo
        """
        # Find last successful move operation
        for entry in reversed(self.entries):
            if entry.success and entry.action == "move":
                old_path = Path(entry.old_path)
                new_path = Path(entry.new_path)
                
                # Check if we can undo
                if new_path.exists() and not old_path.exists():
                    try:
                        # Move file back
                        old_path.parent.mkdir(parents=True, exist_ok=True)
                        new_path.rename(old_path)
                        
                        # Add undo entry to history
                        self.add_entry(
                            new_path,
                            old_path,
                            "undo",
                            True,
                        )
                        
                        return entry
                    except Exception as e:
                        print(f"Error undoing rename: {e}")
                        return None
        
        return None
    
    def clear(self):
        """Clear all history."""
        with self._lock:
            self.entries = []
            self._save()


# Global history instance (initialized in main.py)
history: Optional[RenameHistory] = None
