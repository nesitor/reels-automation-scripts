"""SQLite helpers. WAL mode + safe concurrent readers."""
from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from .config import CFG

SCHEMA_FILE = CFG.root / "db" / "schema.sql"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def connect(*, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Yield a connection in WAL mode with foreign keys and Row factory.

    The connection commits on clean exit and rolls back on exception.
    """
    _ensure_parent(CFG.state_db_path)
    if readonly:
        conn = sqlite3.connect(
            f"file:{CFG.state_db_path}?mode=ro", uri=True, timeout=30.0
        )
    else:
        conn = sqlite3.connect(CFG.state_db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("BEGIN;")
    try:
        yield conn
        # executescript() and a few other ops auto-commit, leaving us out of
        # a transaction. Only COMMIT when we still have one open.
        if not readonly and conn.in_transaction:
            conn.execute("COMMIT;")
    except Exception:
        if not readonly and conn.in_transaction:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()


def init_schema() -> None:
    """Run schema.sql. Safe to call repeatedly (all statements use IF NOT EXISTS)."""
    _ensure_parent(CFG.state_db_path)
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    with connect() as conn:
        conn.executescript(ddl)
