"""
Durable background-job store (review item R2).

Embed jobs (Phase-2 metadata writing, both batch-rename and manual-save) used to
live only in a process-local dict (``_embed_jobs``). That meant:
  • a page refresh lost the handle to an in-flight job, and
  • a server restart turned every ``/api/embed-status/{id}`` poll into a 404, so
    the UI's progress banner could hang or silently disappear.

This adds a tiny SQLite-backed mirror of that job state so progress survives both.
It does **not** try to resume the FFmpeg work itself — a restart kills the running
subprocess, which can't be reattached — so on startup any job still marked
``running`` is flipped to ``interrupted`` (a terminal state the UI can surface
clearly) rather than left polling forever.

Why SQLite (not Redis or another JSON file): it is in the Python standard library
(no dependency, identical on Docker/deb/AppImage), gives durable per-row updates
without rewriting a whole file, and lives in the already-secured ``DATA_DIR``
alongside the history/catalog stores (same permissions/ownership, no secrets).

The in-memory ``_embed_jobs`` dict remains the hot path while the process is
alive; this store is written through on create/progress/finish and is only *read*
as a fallback when the in-memory entry is gone (i.e. after a restart). Single
connection guarded by one lock (the app is pinned to one worker, review item P7);
every method is best-effort and self-disables on failure so a job-store hiccup can
never break embedding.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


_SCHEMA_VERSION = 1
_TERMINAL = ("complete", "interrupted")


class JobStore:
    """Thread-safe, best-effort SQLite mirror of background-job progress."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        id         TEXT PRIMARY KEY,
                        kind       TEXT,
                        total      INTEGER NOT NULL DEFAULT 0,
                        done       INTEGER NOT NULL DEFAULT 0,
                        warnings   TEXT NOT NULL DEFAULT '[]',
                        status     TEXT NOT NULL DEFAULT 'running',
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    )
                    """
                )
                self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
                self._conn.commit()
        except Exception as e:  # pragma: no cover - must never raise
            print(f"WARNING: job store init failed ({e}); durable jobs disabled")
            self._conn = None

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def create(self, job_id: str, kind: str, total: int, complete: bool = False) -> None:
        if not self._conn or not job_id:
            return
        now = time.time()
        try:
            with self._lock:
                self._conn.execute(
                    """INSERT OR REPLACE INTO jobs
                       (id, kind, total, done, warnings, status, created_at, updated_at)
                       VALUES (?, ?, ?, 0, '[]', ?, ?, ?)""",
                    (job_id, kind, total,
                     "complete" if complete else "running", now, now),
                )
                self._conn.commit()
        except Exception as e:
            print(f"WARNING: job store create failed: {e}")

    def progress(self, job_id: str, done: int, warnings: list) -> None:
        if not self._conn or not job_id:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE jobs SET done=?, warnings=?, updated_at=? WHERE id=?",
                    (done, json.dumps(warnings or []), time.time(), job_id),
                )
                self._conn.commit()
        except Exception as e:
            print(f"WARNING: job store progress failed: {e}")

    def finish(self, job_id: str, status: str = "complete") -> None:
        if not self._conn or not job_id:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE jobs SET status=?, updated_at=? WHERE id=?",
                    (status, time.time(), job_id),
                )
                self._conn.commit()
        except Exception as e:
            print(f"WARNING: job store finish failed: {e}")

    def get(self, job_id: str) -> Optional[dict]:
        """Return job state in the same shape the API serves, or None."""
        if not self._conn or not job_id:
            return None
        try:
            with self._lock:
                r = self._conn.execute(
                    "SELECT * FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
        except Exception as e:
            print(f"WARNING: job store get failed: {e}")
            return None
        if not r:
            return None
        try:
            warnings = json.loads(r["warnings"])
        except Exception:
            warnings = []
        return {
            "total": r["total"],
            "done": r["done"],
            "warnings": warnings,
            "status": r["status"],
            "complete": r["status"] in _TERMINAL,
        }

    def interrupt_running(self) -> int:
        """Flip any ``running`` job to ``interrupted`` (call once at startup).

        Returns the number of jobs updated. A process restart kills the embedding
        subprocess, so a still-``running`` row is stale work that can't continue —
        marking it terminal lets the UI stop polling and show a clear state.
        """
        if not self._conn:
            return 0
        try:
            with self._lock:
                cur = self._conn.execute(
                    "UPDATE jobs SET status='interrupted', updated_at=? "
                    "WHERE status='running'",
                    (time.time(),),
                )
                self._conn.commit()
                return cur.rowcount or 0
        except Exception as e:
            print(f"WARNING: job store interrupt_running failed: {e}")
            return 0

    def prune(self, ttl_seconds: float) -> None:
        """Delete terminal jobs older than ``ttl_seconds`` (bounds table growth)."""
        if not self._conn:
            return
        try:
            cutoff = time.time() - ttl_seconds
            with self._lock:
                self._conn.execute(
                    f"DELETE FROM jobs WHERE status IN ({','.join('?' for _ in _TERMINAL)}) "
                    "AND updated_at < ?",
                    (*_TERMINAL, cutoff),
                )
                self._conn.commit()
        except Exception as e:
            print(f"WARNING: job store prune failed: {e}")

    def close(self) -> None:
        if self._conn:
            try:
                with self._lock:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
