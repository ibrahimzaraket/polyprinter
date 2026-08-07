"""SQLite connection handling — WAL mode, single-writer discipline.

Per docs/SCHEMA.md invariant 5: Scout owns traders/snapshots/mandates;
Mirror owns observed_trades/decisions/positions; Learner owns outcomes;
Dashboard reads only. This module doesn't enforce that at the SQL level
(SQLite can't easily express per-table write ACLs) — it's a discipline each
caller must respect. Code review, not a constraint, catches violations.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from polyprinter.config import REPO_ROOT, load_config
from polyprinter.db.migrate import run_migrations


def _default_db_path() -> Path:
    # Relative to REPO_ROOT (POLYPRINTER_HOME / cwd), NOT __file__ — see
    # config.py for why __file__-relative paths break under pip install.
    rel = load_config().get("db", {}).get("path", "data/polyprinter.db")
    return REPO_ROOT / rel


DEFAULT_DB_PATH = _default_db_path()


def get_connection(db_path: str | Path = DEFAULT_DB_PATH, *, migrate: bool = True) -> sqlite3.Connection:
    """Open a WAL-mode connection. Creates the db file and parent dir if absent.

    Safe to call from multiple processes/services concurrently — WAL mode
    allows one writer + many readers without blocking, and migration apply
    uses CREATE TABLE/INDEX IF NOT EXISTS so a race at startup is harmless.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    if migrate:
        run_migrations(conn)

    return conn


@contextmanager
def connection(db_path: str | Path = DEFAULT_DB_PATH, *, migrate: bool = True) -> Iterator[sqlite3.Connection]:
    """Context-manager form: `with connection() as conn: ...`."""
    conn = get_connection(db_path, migrate=migrate)
    try:
        yield conn
    finally:
        conn.close()
