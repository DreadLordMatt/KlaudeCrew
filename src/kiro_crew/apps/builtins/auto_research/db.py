"""Auto-research SQLite connection + one-time schema init.

Depends only on ``constants`` (for ``DB_PATH``). Owns the process-wide
DB-init guards ``_DB_INIT_LOCK`` / ``_INITIALIZED_DBS``.
"""

from __future__ import annotations

import logging
import sqlite3
import threading

from kiro_crew.apps.builtins.auto_research.constants import DB_PATH

logger = logging.getLogger(__name__)

# Serializes the one-time WAL switch + schema init per DB file (see
# _ensure_schema). Keyed by DB path so per-test temp DBs each init once.
_DB_INIT_LOCK = threading.Lock()
_INITIALIZED_DBS: set[str] = set()


def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Explicit 30s busy timeout (vs the 5s driver default). The research worker
    # writes findings/status every cycle while the app's HTTP handlers also
    # read/write; the longer busy timeout absorbs brief write contention instead
    # of surfacing "database is locked". WAL journal mode is set once per DB in
    # _ensure_schema() below (it is persistent in the DB header).
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    # Belt-and-suspenders: also set busy_timeout via PRAGMA so it applies even if
    # a driver ignores the connect kwarg. Neither this nor connect() acquires a
    # DB lock, so it is safe before the schema init runs.
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Switch the DB into WAL mode and create/migrate the schema -- exactly once
    per DB file, serialized by a process-wide lock.

    ``journal_mode=WAL`` is persistent in the DB header, and *switching into*
    WAL needs a brief exclusive lock. Running that switch on every connection
    raced with concurrent writers (validate/create run off the event loop via
    run_in_executor) and surfaced "database is locked" on the PRAGMA itself --
    ``busy_timeout`` cannot resolve exclusive-lock contention where several
    connections all try to flip a not-yet-WAL DB at once. Performing it once,
    under a Python-level lock, guarantees a single connection does the switch
    while no other connection holds a DB lock; later connections find WAL
    already set and skip straight to serving queries. Keyed by DB path so
    per-test temp DBs each initialize independently.
    """
    key = str(DB_PATH)
    if key in _INITIALIZED_DBS and DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        return
    with _DB_INIT_LOCK:
        if key in _INITIALIZED_DBS and DB_PATH.exists() and DB_PATH.stat().st_size > 0:
            return  # double-checked locking
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS campaigns (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, question TEXT NOT NULL,
                sub_questions TEXT NOT NULL DEFAULT '[]', sources TEXT NOT NULL DEFAULT '[]',
                max_cycles INTEGER NOT NULL DEFAULT 30, idle_secs INTEGER NOT NULL DEFAULT 120,
                status TEXT NOT NULL DEFAULT 'ready',
                created_at REAL NOT NULL, started_at REAL, completed_at REAL,
                total_cycles INTEGER NOT NULL DEFAULT 0, error_message TEXT,
                success_criteria TEXT, auto_approve INTEGER NOT NULL DEFAULT 0)"""
            )
            # Migrate DBs created before later columns were added.
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(campaigns)")}
            if "success_criteria" not in cols:
                conn.execute("ALTER TABLE campaigns ADD COLUMN success_criteria TEXT")
            if "auto_approve" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN auto_approve INTEGER NOT NULL DEFAULT 0"
                )
            if "parent_id" not in cols:
                conn.execute("ALTER TABLE campaigns ADD COLUMN parent_id TEXT")
            if "scope_constraints" not in cols:
                conn.execute("ALTER TABLE campaigns ADD COLUMN scope_constraints TEXT")
            if "parallel_workers" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN parallel_workers "
                    "INTEGER NOT NULL DEFAULT 1"
                )
            if "report_artifact_slug" not in cols:
                conn.execute("ALTER TABLE campaigns ADD COLUMN report_artifact_slug TEXT")
            # RL v2: dual execution mode + recursive-exploration budget. NOT NULL
            # with a DEFAULT so existing rows backfill automatically (DEFAULTs
            # mirror the DEFAULT_* constants above).
            if "execution_mode" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'agent'"
                )
            if "max_subquestions_per_round" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN max_subquestions_per_round "
                    "INTEGER NOT NULL DEFAULT 3"
                )
            if "depth_decay" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN depth_decay REAL NOT NULL DEFAULT 0.5"
                )
            if "reserve_fraction" not in cols:
                conn.execute(
                    "ALTER TABLE campaigns ADD COLUMN reserve_fraction REAL NOT NULL DEFAULT 0.15"
                )
            conn.commit()
            _INITIALIZED_DBS.add(key)
        except Exception:
            conn.rollback()
            raise
