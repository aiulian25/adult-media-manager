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

import os
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
    "phash",
    "size",
    "duration",
    "normalized_filename",
    "extracted_date",
)

# Near-duplicate (pHash) grouping tunables (used by find_duplicates).
#   _PHASH_HAMMING_MAX   : max bit difference (of 64) still counted as "similar".
#                          8 tolerates re-encode/resize noise without merging
#                          unrelated scenes.
#   _PHASH_GROUP_MAX_ROWS: safety cap — the near-dup pass is O(n²) pairwise, so
#                          above this many pHash rows we skip it (return only the
#                          exact-oshash groups) rather than risk a long UI stall.
_PHASH_HAMMING_MAX = 8
_PHASH_GROUP_MAX_ROWS = 20000


def _split_sizes(raw) -> list[int]:
    """Split a ``GROUP_CONCAT(size, char(10))`` blob into a list of ints.

    Positionally parallel to the same row's ``GROUP_CONCAT(path)``. Any
    unparseable entry becomes 0 so the list length always matches the paths list.
    """
    if not raw:
        return []
    out: list[int] = []
    for part in str(raw).split("\n"):
        try:
            out.append(int(part))
        except (ValueError, TypeError):
            out.append(0)
    return out


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
                e.get("phash"),
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
                        (path, oshash, phash, size, duration, normalized_filename,
                         extracted_date, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        oshash=excluded.oshash,
                        -- Preserve a previously computed pHash when this scan
                        -- didn't compute one (AMM_SCAN_PHASH off): NULL must not
                        -- wipe an existing hash. A pHash-on rescan (non-NULL)
                        -- refreshes it, so in-place re-encodes still update.
                        phash=COALESCE(excluded.phash, files.phash),
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

    def update_fingerprint(self, path: str, oshash: Optional[str],
                           size: Optional[int] = None) -> None:
        """Refresh ONLY the content-fingerprint columns for an existing row.

        Used after a metadata embed rewrites a file's bytes (F6): oshash and
        size change but duration, normalized name, extracted date and the match
        columns do not — so unlike ``upsert_scanned`` (whose upsert overwrites
        those with NULLs when absent) this can never degrade the row. The pHash
        is untouched: tags don't alter decoded video. No-op when the path has
        no row yet — the next scan creates it with correct values anyway.
        """
        if not self._conn or not path or not oshash:
            return
        try:
            with self._lock:
                self._conn.execute(
                    "UPDATE files SET oshash=?, size=COALESCE(?, size), updated_at=? "
                    "WHERE path=?",
                    (oshash, size, time.time(), path),
                )
                self._conn.commit()
        except Exception as e:
            print(f"WARNING: catalog update_fingerprint failed: {e}")

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
        """Return duplicate groups. Each group is
        ``{kind, count, paths, sizes[, oshash]}`` (``sizes`` are per-file byte
        sizes, positionally parallel to ``paths``, for the UI) where ``kind`` is:

          • ``"oshash"`` — byte-identical copies (same content fingerprint).
          • ``"phash"``  — near-duplicates (same scene, different encode/resize):
            pHashes within :data:`_PHASH_HAMMING_MAX` bits that span ≥2 distinct
            oshashes. Only produced when pHashes exist (opt-in AMM_SCAN_PHASH).

        The near-dup pass is O(n²) over pHash rows, so it is skipped above
        :data:`_PHASH_GROUP_MAX_ROWS` (exact groups are still returned).
        """
        if not self._conn:
            return []
        groups: list[dict] = []
        try:
            with self._lock:
                # 1) Exact content dups — identical bytes (grouped in SQL).
                #    paths and sizes are concatenated in the same row-visitation
                #    order, so they line up positionally after the split.
                cur = self._conn.execute(
                    """SELECT oshash,
                              GROUP_CONCAT(path, char(10)) AS paths,
                              GROUP_CONCAT(COALESCE(size, 0), char(10)) AS sizes,
                              COUNT(*) AS n
                       FROM files
                       WHERE oshash IS NOT NULL AND oshash != ''
                       GROUP BY oshash HAVING COUNT(*) > 1
                       ORDER BY n DESC"""
                )
                for r in cur.fetchall():
                    paths = (r["paths"] or "").split("\n")
                    # Hardlink-resolved groups (F16): copies replaced with links
                    # to the kept file all share one inode — no space to reclaim,
                    # so the group is no longer a duplicate. Filtering here (not
                    # at resolve time) makes the fix survive rescans, which
                    # re-upsert the rows without knowing about inodes. stat
                    # errors count as distinct so a flaky mount can't hide dups.
                    inodes = set()
                    distinct = 0
                    for p in paths:
                        try:
                            st = os.stat(p)
                            key = (st.st_dev, st.st_ino)
                        except OSError:
                            key = ("?", p)
                        if key not in inodes:
                            inodes.add(key)
                            distinct += 1
                    if distinct < 2:
                        continue
                    groups.append({
                        "kind": "oshash",
                        "oshash": r["oshash"],
                        "count": r["n"],
                        "paths": paths,
                        "sizes": _split_sizes(r["sizes"]),
                    })

                # 2) Fetch pHash rows for the near-dup pass (done outside the lock).
                phash_rows = self._conn.execute(
                    """SELECT path, oshash, phash, size FROM files
                       WHERE phash IS NOT NULL AND phash != ''"""
                ).fetchall()
        except Exception as e:
            print(f"WARNING: catalog find_duplicates failed: {e}")
            return groups

        groups.extend(self._phash_groups(phash_rows))
        return groups

    @staticmethod
    def _phash_groups(rows) -> list[dict]:
        """Cluster pHash rows into near-duplicate groups (union-find, Hamming ≤ cap).

        Pure/self-contained (no DB, no lock) so it is easy to unit-test. Clusters
        confined to a single oshash are dropped — those are exact copies already
        reported by the oshash pass; a kept cluster spans ≥2 oshashes, i.e. a real
        re-encode. Returns ``[{kind:"phash", count, paths}]``.
        """
        if len(rows) < 2 or len(rows) > _PHASH_GROUP_MAX_ROWS:
            return []

        # Parse the stored pHash strings to ints once (skip unparseable).
        # Legacy frame hashes are decimal; stash-compatible sprite hashes (F14)
        # are lowercase hex. Try decimal first, hex second — a sprite hash with
        # no a-f digits (≈0.05% of values) mis-parses as decimal, which at worst
        # weakens one near-dup comparison; cross-algorithm comparisons are
        # meaningless anyway and rescans refresh old values per file.
        # Each item: (path, oshash, phash_int, size).
        items: list[tuple[str, object, int, int]] = []
        for r in rows:
            try:
                ph = str(r["phash"])
                try:
                    ph_int = int(ph)
                except ValueError:
                    ph_int = int(ph, 16)
                items.append((r["path"], r["oshash"], ph_int,
                              int(r["size"] or 0)))
            except (ValueError, TypeError):
                continue

        n = len(items)
        parent = list(range(n))

        def _find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def _union(a: int, b: int) -> None:
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[rb] = ra

        for i in range(n):
            hi = items[i][2]
            for j in range(i + 1, n):
                # Hamming distance via popcount of XOR (bin().count is 3.9-safe).
                if bin(hi ^ items[j][2]).count("1") <= _PHASH_HAMMING_MAX:
                    _union(i, j)

        clusters: dict[int, list[int]] = {}
        for i in range(n):
            clusters.setdefault(_find(i), []).append(i)

        out: list[dict] = []
        for members in clusters.values():
            if len(members) < 2:
                continue
            # Keep only clusters that mix distinct oshashes (true re-encodes);
            # a single-oshash cluster is just the exact dups already reported.
            oshashes = {items[m][1] for m in members if items[m][1]}
            if len(oshashes) < 2:
                continue
            out.append({
                "kind": "phash",
                "count": len(members),
                "paths": [items[m][0] for m in members],
                "sizes": [items[m][3] for m in members],
            })
        return out

    def close(self) -> None:
        if self._conn:
            try:
                with self._lock:
                    self._conn.close()
            except Exception:
                pass
            self._conn = None
