"""SQLite connection management.

One small helper: open a connection (creating the database file's
parent directory and tables on first use), with row access by column
name and sane defaults for a single-writer app with one background
sync process and one query/response process.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import DATABASE_PATH
from app.database.schema import init_db


def _connect(database_path: str) -> sqlite3.Connection:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path)
    conn.row_factory = sqlite3.Row
    # A busy app (Gmail sync running while a founder query comes in) can
    # hit "database is locked" under SQLite's single-writer model; a
    # busy timeout makes concurrent access retry briefly instead of
    # failing immediately.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    init_db(conn)
    return conn


@contextmanager
def get_connection(database_path: str = DATABASE_PATH) -> Iterator[sqlite3.Connection]:
    """Context manager yielding a ready-to-use SQLite connection, with
    tables already created. Commits on clean exit, rolls back on
    exception, always closes the connection.

    `database_path` defaults to app.config.DATABASE_PATH but can be
    overridden -- tests pass a temp-file or in-memory path so they
    never touch the real database.
    """
    conn = _connect(database_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
