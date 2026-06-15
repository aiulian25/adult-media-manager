"""
SQLite file→match catalog (review item R1).

An *additive* persistence layer that records what AMM knows about each media file
on disk: its content fingerprint, derived metadata, and — once organised — the
canonical scene it was matched to and whether the user confirmed it. This is what
makes re-scans *incremental*: files AMM has already organised can be skipped, and
duplicate content (same oshash, different paths) can be surfaced.

Why SQLite (not another JSON file): the catalog is the one store that benefits
from indexed lookups (by path and by oshash) and partial updates without
rewriting the whole file. ``sqlite3`` is in the Python standard library, so this
adds **no** dependency and behaves identically on Docker, deb and AppImage. The
database lives in ``DATA_DIR`` alongside the existing JSON stores (history,
settings, …), so it inherits the same directory permissions and ownership
(PUID/PGID) — no new secrets and no new write paths outside the secured data dir.

The already-hardened JSON stores (history, settings, user_tags, known_sites,
match_cache) are intentionally left in place; this module only adds the new
file→match catalog rather than re-doing working, recently-hardened code.

Concurrency: the app is pinned to a single worker (review item P7), but its sync
handlers run in FastAPI's thread-pool, so catalog calls can still arrive from
several threads. A single connection (``check_same_thread=False``) is guarded by
one re-entrant lock, and WAL mode is enabled for durability. Every public method
is best-effort: a catalogue failure must never break a scan or a rename, so
errors are swallowed (and logged) and a safe default is returned.
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional


_SCHEMA_VERSION = 1

# Columns observed purely from scanning a file on disk (no match involved). These
# are refreshed on every scan; the match columns below are preserved across scans.
_SCAN_COLUMNS = (
    "oshash",
    "size",
    "duration",
    "normalized_filename",
    "extracted_date",
)


class Catalog:
    """Thread-safe, best-effort SQLite catalog of files AMM has seen/organised."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        try:
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._init_schema()
        except Exception as e:  # pragma: no cover - construction must never raise
            print(f"WARNING: catalog init failed ({e}); catalog disabled")
            self._conn = None

    # ── schema ────────────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        c = self._conn
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                path                TEXT PRIMARY KEY,
                oshash              TEXT,
                phash               TEXT,
                size                INTEGER,
                duration            REAL,
                normalized_filename TEXT,
                extracted_date      TEXT,
                source_system       TEXT,
                canonical_scene_id  TEXT,
                confidence_score    REAL,
                user_confirmed      INTEGER NOT NULL DEFAULT 0,
                organized           INTEGER NOT NULL DEFAULT 0,
                matched_at          REAL,
                updated_at          REAL NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_files_oshash ON files(oshash)")
        c.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        c.commit()

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    # ── scan-time upsert ────────────────────────────────────────────────────
    def upsert_scanned(self, entries: list[dict]) -> None:
        """Record/refresh the observed (on-disk) metadata for scanned files.

        Only the scan-derived columns are written; the match columns
        (organized / user_confirmed / canonical_scene_id / …) are *preserved* so a
        re-scan never forgets a previously-confirmed match. One transaction for the
        whole batch (mirrors the batched history write, review item P5).
        """
        if not self._conn or not entries:
            return
        now = time.time()
        rows = [
            (
                e.get("path"),
                e.get("oshash"),
                e.get("size"),
                e.get("duration_seconds"),
                e.get("normalized_name"),
                e.get("release_date"),
                now,
            )
            for e in entries
            if e.get("path")
        ]
        if not rows:
            return
        try:
            with self._lock:
                self._conn.executemany(
                    """
                    INSERT INTO files
                        (path, oshash, size, duration, normalized_filename,
                         extracted_date, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        oshash=excluded.oshash,
                        size=excluded.size,
                        duration=excluded.duration,
                        normalized_filename=excluded.normalized_filename,
                        extracted_date=excluded.extracted_date,
                        updated_at=excluded.updated_at
                    """,
                    rows,
                )
                self._conn.commit()
        except Exception as e:
            print(f"WARNING: catalog upsert_scanned failed: {e}")

    def get_states(self, paths: list[str]) -> dict[str, dict]:
        """Return per-path match state for the given paths (only those present).

        Shape: ``{path: {organized, user_confirmed, canonical_scene_id,
        source_system, confidence_score}}``. Used by scan to annotate / skip files
        AMM has already organised.
        """
        if not self._conn or not paths:
            return {}
        out: dict[str, dict] = {}
        try:
            with self._lock:
                # Chunk to stay well under SQLite's variable limit (999).
                for i in range(0, len(paths), 500):
                    chunk = paths[i : i + 500]
                    q = ",".join("?" for _ in chunk)
                    cur = self._conn.execute(
                        f"""SELECT path, organized, user_confirmed,
                                   canonical_scene_id, source_system, confidence_score
                            FROM files WHERE path IN ({q})""",
                        chunk,
                    )
                    for r in cur.fetchall():
                        out[r["path"]] = {
                            "organized": bool(r["organized"]),
                            "user_confirmed": bool(r["user_confirmed"]),
                            "canonical_scene_id": r["canonical_scene_id"],
                            "source_system": r["source_system"],
                            "confidence_score": r["confidence_score"],
                        }
        except Exception as e:
            print(f"WARNING: catalog get_states failed: {e}")
        return out

    # ── match-time updates ──────────────────────────────────────────────────
    def mark_organized(
        self,
        path: str,
        *,
        oshash: Optional[str] = None,
        scene_id: Optional[str] = None,
        source: Optional[str] = None,
        confidence: Optional[float] = None,
        confirmed: bool = False,
    ) -> None:
        """Mark ``path`` as organised by AMM and attach its canonical match.

        Never downgrades an existing ``user_confirmed`` flag, and only overwrites
        match fields with non-NULL values (COALESCE), so a later, sparser update
        can't wipe richer data.
        """
        if not self._conn or not path:
            return
        now = time.time()
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO files
                        (path, oshash, source_system, canonical_scene_id,
                         confidence_score, user_confirmed, organized,
                         matched_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        oshash=COALESCE(excluded.oshash, files.oshash),
                        source_system=COALESCE(excluded.source_system, files.source_system),
                        canonical_scene_id=COALESCE(excluded.canonical_scene_id, files.canonical_scene_id),
                        confidence_score=COALESCE(excluded.confidence_score, files.confidence_score),
                        user_confirmed=MAX(files.user_confirmed, excluded.user_confirmed),
                        organized=1,
                        matched_at=excluded.matched_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        path,
                        oshash,
                        source,
                        str(scene_id) if scene_id is not None else None,
                        confidence,
                        1 if confirmed else 0,
                        now,
                        now,
                    ),
                )
                self._conn.commit()
        except Exception as e:
            print(f"WARNING: catalog mark_organized failed: {e}")

    def set_organized(self, path: str, organized: bool) -> None:
        """Flip ONLY the organised flag for ``path`` (keeps match/confirm data).

        Used by the scan to self-heal a stale row: if the catalog says a file is
        organised but its NFO sidecar is gone from disk (an embed that never
        completed, or a user-deleted NFO), the filesystem is authoritative and
        the flag is cleared — without discarding the canonical match/confirm
        state, which ``forget`` would.
        """
        if not self._conn or not path:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE files SET organized=?, updated_at=? WHERE path=?",
                    (1 if organized else 0, time.time(), path),
                )
                self._conn.commit()
        except Exception as e:
            print(f"WARNING: catalog set_organized failed: {e}")

    def forget(self, path: str) -> None:
        """Drop the row for ``path`` (e.g. the source side of a move)."""
        if not self._conn or not path:
            return
        try:
            with self._lock:
                self._conn.execute("DELETE FROM files WHERE path=?", (path,))
                self._conn.commit()
        except Exception as e:
            print(f"WARNING: catalog forget failed: {e}")

    # ── reporting ───────────────────────────────────────────────────────────
    def stats(self) -> dict:
        """Aggregate counts for the UI: total tracked / organised / confirmed."""
        if not self._conn:
            return {"total": 0, "organized": 0, "confirmed": 0, "duplicates": 0}
        try:
            with self._lock:
                row = self._conn.execute(
                    """SELECT COUNT(*) AS total,
                              COALESCE(SUM(organized), 0) AS organized,
                              COALESCE(SUM(user_confirmed), 0) AS confirmed
                       FROM files"""
                ).fetchone()
                dups = self._conn.execute(
                    """SELECT COUNT(*) AS n FROM (
                           SELECT oshash FROM files
                           WHERE oshash IS NOT NULL AND oshash != ''
                           GROUP BY oshash HAVING COUNT(*) > 1
                       )"""
                ).fetchone()
            return {
                "total": row["total"],
                "organized": row["organized"],
                "confirmed": row["confirmed"],
                "duplicates": dups["n"],
            }
        except Exception as e:
            print(f"WARNING: catalog stats failed: {e}")
            return {"total": 0, "organized": 0, "confirmed": 0, "duplicates": 0}

    def find_duplicates(self) -> list[dict]:
        """Return groups of paths that share an oshash (same content, ≥2 copies)."""
        if not self._conn:
            return []
        try:
            with self._lock:
                cur = self._conn.execute(
                    """SELECT oshash, GROUP_CONCAT(path, char(10)) AS paths,
                              COUNT(*) AS n
                       FROM files
                       WHERE oshash IS NOT NULL AND oshash != ''
                       GROUP BY oshash HAVING COUNT(*) > 1
                       ORDER BY n DESC"""
                )
                return [
                    {
                        "oshash": r["oshash"],
                        "count": r["n"],
                        "paths": (r["paths"] or "").split("\n"),
                    }
                    for r in cur.fetchall()
                ]
        except Exception as e:
            print(f"WARNING: catalog find_duplicates failed: {e}")
            return []

    def close(self) -> None:
        if self._conn:
            try:
                with self._lock:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
