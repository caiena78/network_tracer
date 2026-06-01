"""
tracer_api/history.py
=====================
SQLite-backed trace history persistence.

Table: trace_history
  id           TEXT PRIMARY KEY   -- UUID4 string
  src_ip       TEXT NOT NULL
  dst_ip       TEXT NOT NULL
  created_at   TEXT NOT NULL      -- ISO 8601 UTC
  status       TEXT NOT NULL      -- "completed" | "failed"
  duration_s   REAL               -- nullable
  flat_paths   TEXT               -- JSON-encoded list[dict] (raw trace result)
  graph_json   TEXT               -- JSON-encoded built graph (elements+paths+metadata)

The module exposes a module-level singleton ``get_history_db()`` that is
lazily initialised from ``settings.history_db_path`` on first access.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS trace_history (
    id           TEXT PRIMARY KEY,
    src_ip       TEXT NOT NULL,
    dst_ip       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL,
    duration_s   REAL,
    flat_paths   TEXT,
    graph_json   TEXT
);
"""
_CREATE_IDX_SRC = "CREATE INDEX IF NOT EXISTS idx_hist_src ON trace_history(src_ip);"
_CREATE_IDX_DST = "CREATE INDEX IF NOT EXISTS idx_hist_dst ON trace_history(dst_ip);"
_CREATE_IDX_TS  = "CREATE INDEX IF NOT EXISTS idx_hist_ts  ON trace_history(created_at DESC);"


# ---------------------------------------------------------------------------
# HistoryEntry data class
# ---------------------------------------------------------------------------

class HistoryEntry:
    """Lightweight data holder for one history record."""

    __slots__ = (
        "id", "src_ip", "dst_ip", "created_at",
        "status", "duration_s", "flat_paths", "graph_json",
    )

    def __init__(
        self,
        id:          str,
        src_ip:      str,
        dst_ip:      str,
        created_at:  str,
        status:      str,
        duration_s:  Optional[float],
        flat_paths:  Optional[List[dict]],
        graph_json:  Optional[dict],
    ) -> None:
        self.id         = id
        self.src_ip     = src_ip
        self.dst_ip     = dst_ip
        self.created_at = created_at
        self.status     = status
        self.duration_s = duration_s
        self.flat_paths = flat_paths
        self.graph_json = graph_json

    def to_summary_dict(self) -> Dict[str, Any]:
        return {
            "id":         self.id,
            "src_ip":     self.src_ip,
            "dst_ip":     self.dst_ip,
            "created_at": self.created_at,
            "status":     self.status,
            "duration_s": self.duration_s,
        }

    def to_detail_dict(self) -> Dict[str, Any]:
        d = self.to_summary_dict()
        d["graph"] = self.graph_json
        return d


# ---------------------------------------------------------------------------
# HistoryDB
# ---------------------------------------------------------------------------

class HistoryDB:
    """
    Thread-safe SQLite persistence layer for trace history.

    Uses thread-local connections (one connection per thread) and a write
    lock to serialise INSERT / DELETE operations.  Reads are lock-free.
    """

    def __init__(self, db_path: str) -> None:
        self._path  = db_path
        self._wlock = threading.Lock()          # serialise writes
        self._local = threading.local()         # per-thread connections

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Return (or create) the thread-local SQLite connection."""
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Create schema on startup.  Idempotent."""
        with self._wlock:
            conn = self._conn()
            conn.execute(_CREATE_TABLE)
            conn.execute(_CREATE_IDX_SRC)
            conn.execute(_CREATE_IDX_DST)
            conn.execute(_CREATE_IDX_TS)
            conn.commit()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def save(
        self,
        src_ip:     str,
        dst_ip:     str,
        status:     str,
        created_at: Optional[str]        = None,
        duration_s: Optional[float]      = None,
        flat_paths: Optional[List[dict]] = None,
        graph_json: Optional[dict]       = None,
    ) -> str:
        """Insert a new history entry.  Returns the generated UUID."""
        entry_id  = str(uuid.uuid4())
        ts        = created_at or datetime.now(timezone.utc).isoformat()
        fp_str    = json.dumps(flat_paths,  separators=(",", ":")) if flat_paths  is not None else None
        gj_str    = json.dumps(graph_json,  separators=(",", ":")) if graph_json  is not None else None

        with self._wlock:
            conn = self._conn()
            conn.execute(
                """INSERT INTO trace_history
                     (id, src_ip, dst_ip, created_at, status, duration_s, flat_paths, graph_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry_id, src_ip, dst_ip, ts, status, duration_s, fp_str, gj_str),
            )
            conn.commit()
        return entry_id

    def delete(self, entry_id: str) -> bool:
        """Delete one entry.  Returns True when a row was removed."""
        with self._wlock:
            conn = self._conn()
            cur  = conn.execute(
                "DELETE FROM trace_history WHERE id = ?", (entry_id,)
            )
            conn.commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, entry_id: str) -> Optional[HistoryEntry]:
        """Fetch a single entry by primary key (includes graph payload)."""
        row = self._conn().execute(
            "SELECT * FROM trace_history WHERE id = ?", (entry_id,)
        ).fetchone()
        return self._row_to_entry(row, full=True) if row else None

    def list(
        self,
        src_ip: Optional[str] = None,
        dst_ip: Optional[str] = None,
        q:      Optional[str] = None,
        limit:  int           = 100,
        offset: int           = 0,
    ) -> List[HistoryEntry]:
        """
        List entries, newest first.  Payload (flat_paths/graph_json) is omitted
        for efficiency; use ``get()`` to retrieve the full entry.

        Filtering
        ---------
        src_ip : substring match on src_ip column
        dst_ip : substring match on dst_ip column
        q      : substring match on src_ip OR dst_ip OR created_at
        """
        clauses: List[str] = []
        params:  List[Any] = []

        if src_ip:
            clauses.append("src_ip LIKE ?")
            params.append(f"%{src_ip}%")
        if dst_ip:
            clauses.append("dst_ip LIKE ?")
            params.append(f"%{dst_ip}%")
        if q:
            clauses.append("(src_ip LIKE ? OR dst_ip LIKE ? OR created_at LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql   = f"""
            SELECT id, src_ip, dst_ip, created_at, status, duration_s
            FROM trace_history
            {where}
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """
        params += [limit, offset]

        rows = self._conn().execute(sql, params).fetchall()
        return [self._row_to_entry(r, full=False) for r in rows]

    def count(
        self,
        src_ip: Optional[str] = None,
        dst_ip: Optional[str] = None,
        q:      Optional[str] = None,
    ) -> int:
        clauses: List[str] = []
        params:  List[Any] = []
        if src_ip:
            clauses.append("src_ip LIKE ?")
            params.append(f"%{src_ip}%")
        if dst_ip:
            clauses.append("dst_ip LIKE ?")
            params.append(f"%{dst_ip}%")
        if q:
            clauses.append("(src_ip LIKE ? OR dst_ip LIKE ? OR created_at LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        row   = self._conn().execute(
            f"SELECT COUNT(*) FROM trace_history {where}", params
        ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Mapping helper
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_entry(row: sqlite3.Row, full: bool = True) -> HistoryEntry:
        keys = row.keys()
        fp_raw = row["flat_paths"] if full and "flat_paths" in keys else None
        gj_raw = row["graph_json"] if full and "graph_json" in keys else None
        return HistoryEntry(
            id         = row["id"],
            src_ip     = row["src_ip"],
            dst_ip     = row["dst_ip"],
            created_at = row["created_at"],
            status     = row["status"],
            duration_s = row["duration_s"],
            flat_paths = json.loads(fp_raw) if fp_raw else None,
            graph_json = json.loads(gj_raw) if gj_raw else None,
        )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_history_db: Optional[HistoryDB] = None
_init_lock  = threading.Lock()


def get_history_db() -> HistoryDB:
    """Return the module-level singleton, initialising it on first call."""
    global _history_db
    if _history_db is None:
        with _init_lock:
            if _history_db is None:
                from .config import settings
                db = HistoryDB(settings.history_db_path)
                db.init()
                _history_db = db
    return _history_db
