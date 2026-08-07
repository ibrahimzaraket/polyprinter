"""Tiny forward-only migration runner.

Migration 1 applies db/schema.sql (rewritten with IF NOT EXISTS so it's
idempotent) — see db/migrations/0001_init.sql for why that file is a pointer
rather than a duplicate. Migrations 2+ are literal numbered .sql files in
db/migrations/, applied in order, each in its own transaction.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
SCHEMA_SQL_PATH = Path(__file__).resolve().parent / "schema.sql"

_CREATE_RE = re.compile(r"\bCREATE (TABLE|INDEX)\b(?! IF NOT EXISTS)", re.IGNORECASE)


def _idempotent_schema_sql() -> str:
    return _CREATE_RE.sub(lambda m: f"CREATE {m.group(1)} IF NOT EXISTS", SCHEMA_SQL_PATH.read_text())


def _numbered_migration_files() -> list[tuple[int, Path]]:
    files = []
    for path in MIGRATIONS_DIR.glob("*.sql"):
        m = re.match(r"^(\d+)_", path.name)
        if m:
            files.append((int(m.group(1)), path))
    return sorted(files)


def run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL
        )
        """
    )

    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}

    if 1 not in applied:
        with conn:  # transaction
            conn.executescript(_idempotent_schema_sql())
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (1, datetime('now'))"
            )

    for version, path in _numbered_migration_files():
        if version == 1 or version in applied:
            continue
        with conn:
            conn.executescript(path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                (version,),
            )
