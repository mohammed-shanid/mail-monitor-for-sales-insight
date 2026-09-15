"""SQLite connection management.

One small helper: open a connection (creating the database file's
parent directory and tables on first use), with row access by column
name and sane defaults for a single-writer app with one background
sync process and one query/response process.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import app.config as config
from app.config import DATABASE_PATH
from app.database import schema
from app.database.schema import init_db

logger = logging.getLogger(__name__)


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


# =============================================================================
# Report path (SPEC.md §14), added Stage 2. `get_connection()`/`_connect()`
# above are UNCHANGED and still used by the bot exclusively -- they always
# create/open the bot's old-shaped tables via `schema.init_db()`.
# `get_report_connection()` is the report path's own, separate connection
# path: WAL mode, foreign keys on, and `schema.migrate()` (the SPEC.md
# §14.2 schema) instead of `init_db()`. See app.database.schema module
# docstring for why these are two different functions rather than one
# changed in place.
# =============================================================================


class DatabaseError(RuntimeError):
    """Raised on a DB-level failure the report path cannot recover from
    -- report.py maps this to exit code 6 (SPEC.md §13.3, §17). Never
    raised for an ordinary "no rows" result; only for locked/corrupt
    files or a failed backup verification.
    """


def _resolve_database_path(database_path: Optional[str]) -> str:
    # Read app.config live (not bound at import time) so a test that
    # monkeypatches app.config.DATABASE_PATH is honoured even without a
    # reload -- see the timewindow/report.py lesson from Stage 1.
    return database_path if database_path is not None else config.DATABASE_PATH


def _backup_before_migration(database_path: str) -> None:
    """Copy the DB file to `<path>.bak.<unix_ts>` and verify the backup
    is non-zero before any destructive rebuild touches the original
    (SPEC.md §14.1, PHASE0_DECISIONS.md Q3). Called only when the file
    already exists and holds the bot's old-shaped tables.
    """
    backup_path = f"{database_path}.bak.{int(time.time())}"
    shutil.copy2(database_path, backup_path)

    backup_size = Path(backup_path).stat().st_size
    if backup_size <= 0:
        raise DatabaseError(
            f"Migration backup verification failed: {backup_path} is empty"
        )
    logger.info("Backed up %s to %s (%d bytes) before schema migration", database_path, backup_path, backup_size)


def _needs_backup_before_migrating(database_path: str) -> bool:
    """True if `database_path` exists, is non-empty, and holds the
    bot's old-shaped tables -- i.e. migrate() is about to DROP and
    recreate them. Inspects via a short-lived read-only connection so
    nothing is ever held open across the backup copy.
    """
    path = Path(database_path)
    if not path.exists() or path.stat().st_size == 0:
        return False

    uri = f"file:{path}?mode=ro"
    try:
        inspect_conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        raise DatabaseError(f"Could not open database file for inspection: {exc}") from exc

    try:
        return schema.is_legacy_bot_schema(inspect_conn)
    finally:
        inspect_conn.close()


def _connect_report(database_path: str) -> sqlite3.Connection:
    Path(database_path).parent.mkdir(parents=True, exist_ok=True)

    try:
        if _needs_backup_before_migrating(database_path):
            _backup_before_migration(database_path)
    except sqlite3.Error as exc:
        raise DatabaseError(f"Database inspection/backup failed: {exc}") from exc

    try:
        conn = sqlite3.connect(database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        schema.migrate(conn)
    except sqlite3.Error as exc:
        raise DatabaseError(f"Database error: {exc}") from exc

    return conn


@contextmanager
def get_report_connection(database_path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """Context manager for the report path's own database connection
    (SPEC.md §14.2 schema). Backs up and migrates the file in place if
    it still holds the bot's old-shaped tables; otherwise a no-op
    (idempotent, safe to call on every run).

    `database_path` defaults to the live `app.config.DATABASE_PATH`
    when omitted -- never bound at import time.
    """
    resolved_path = _resolve_database_path(database_path)
    conn = _connect_report(resolved_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
