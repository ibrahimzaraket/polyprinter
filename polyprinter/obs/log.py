"""Structured logging — to a file AND to the `events` table (queryable from
the dashboard). Per docs/SCHEMA.md invariant 4-adjacent discipline: this is
the only logging path services should use, so every service's activity is
inspectable from one place without SSH.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polyprinter.config import REPO_ROOT

# Relative to REPO_ROOT, NOT __file__ — see config.py for why.
LOG_DIR = REPO_ROOT / "data"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Logger:
    """Thin wrapper: every call writes to the rotating file logger and
    inserts a row into `events`. `conn` is optional — pass None to log to
    file only (e.g. before the db is ready).
    """

    def __init__(self, service: str, conn: sqlite3.Connection | None = None):
        self.service = service
        self.conn = conn
        self._file_logger = self._make_file_logger(service)

    @staticmethod
    def _make_file_logger(service: str) -> logging.Logger:
        logger = logging.getLogger(f"polyprinter.{service}")
        if logger.handlers:  # already configured (e.g. re-instantiated Logger)
            return logger
        logger.setLevel(logging.DEBUG)
        handler = logging.handlers.RotatingFileHandler(
            LOG_DIR / f"{service}.log", maxBytes=10_000_000, backupCount=5
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        # Also echo to stdout — `docker compose logs` should show activity
        # without needing to exec into the container.
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(stream)
        return logger

    def _emit(self, level: str, message: str, context: dict[str, Any] | None) -> None:
        context = context or {}
        self._file_logger.log(getattr(logging, level), "%s %s", message, json.dumps(context, default=str))

        if self.conn is not None:
            self.conn.execute(
                "INSERT INTO events (ts, service, level, message, context_json) VALUES (?, ?, ?, ?, ?)",
                (_now_iso(), self.service, level, message, json.dumps(context, default=str)),
            )

    def debug(self, message: str, **context: Any) -> None:
        self._emit("DEBUG", message, context)

    def info(self, message: str, **context: Any) -> None:
        self._emit("INFO", message, context)

    def warning(self, message: str, **context: Any) -> None:
        self._emit("WARNING", message, context)

    def error(self, message: str, **context: Any) -> None:
        self._emit("ERROR", message, context)
