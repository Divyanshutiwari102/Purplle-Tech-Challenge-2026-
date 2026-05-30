"""
Database access layer.

Why a thin sqlite3 wrapper instead of SQLAlchemy ORM?
  - The schema is small (6 tables) and all queries are explicit SQL.
  - Tracked decisions, indexes and CHECK constraints live in schema.sql,
    not behind an ORM. This is easier to defend in a code review.
  - We still expose `OperationalError` so the FastAPI middleware can
    convert it to a structured HTTP 503.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# OperationalError is the exception we map to HTTP 503 in the API.
OperationalError = sqlite3.OperationalError

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
_DEFAULT_DB = os.environ.get("STORE_INTEL_DB", "/data/store_intel.db")

# A single connection per thread. SQLite connections are not thread-safe
# by default; using a thread-local keeps things correct without paying
# the cost of a connection pool we don't need at this scale.
_local = threading.local()


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        check_same_thread=False,
        isolation_level=None,   # autocommit; we manage transactions explicitly
        timeout=30.0,           # wait up to 30s on a busy DB before raising
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    db_path = db_path or _DEFAULT_DB
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != db_path:
        conn = _connect(db_path)
        _local.conn = conn
        _local.path = db_path
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create all tables/indexes. Safe to call repeatedly (IF NOT EXISTS)."""
    conn = get_connection(db_path)
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as f:
        conn.executescript(f.read())


@contextmanager
def transaction(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def row_to_event(row: sqlite3.Row) -> dict:
    """Convert a DB row to the event dict shape (metadata parsed)."""
    d = dict(row)
    if "metadata" in d and isinstance(d["metadata"], str):
        try:
            d["metadata"] = json.loads(d["metadata"])
        except json.JSONDecodeError:
            d["metadata"] = {}
    if "is_staff" in d:
        d["is_staff"] = bool(d["is_staff"])
    return d
