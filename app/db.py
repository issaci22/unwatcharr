"""SQLite connection and query helpers.

One connection shared across the app, guarded by a re-entrant lock. The workload
is a handful of queries per page view plus a burst of inserts during a run, so a
connection pool would be pure overhead. WAL keeps the scheduler from blocking
the web UI mid-run -- where the filesystem will accept it.

All SQL lives in store.py. This module owns opening, pragmas, and migration
dispatch only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config
from .timeutil import now as _now

log = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    """Open (once) and return the shared connection, running migrations."""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn

        try:
            config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            _set_journal_mode(conn)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except (sqlite3.Error, OSError) as exc:
            # Almost always a permissions problem on the mounted volume. SQLite's
            # own wording ("disk I/O error") sends people hunting for failing
            # disks, so say what it actually means and crash with a message
            # worth reading.
            raise RuntimeError(
                f"Could not open the database at {config.DB_PATH}: {exc}\n"
                f"The usual cause is that {config.CONFIG_DIR} is not writable by "
                "the user this container runs as. Check that PUID/PGID match the "
                "owner of the directory you mounted ('ls -n' on the host shows "
                "the numeric ids), and that nothing else is holding the database "
                "open."
            ) from exc

        _conn = conn

        from . import migrations

        migrations.apply(conn)
        _harden_permissions()
        return conn


def _set_journal_mode(conn: sqlite3.Connection) -> None:
    """Prefer WAL, but do not die without it.

    WAL needs shared-memory mapping, which SMB and some NFS mounts do not
    support -- and pointing a container's config at a network share is a normal
    thing to do on a NAS. The rollback journal costs a little concurrency and
    keeps the app usable.
    """
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    except sqlite3.Error as exc:
        log.warning("Could not enable WAL (%s); using the default journal.", exc)
        return
    if str(mode).lower() != "wal":
        log.warning(
            "This filesystem would not accept WAL journalling (got %r). Falling "
            "back to the rollback journal -- fine, just slightly less concurrent. "
            "A local disk rather than a network share avoids this.",
            mode,
        )


def _harden_permissions() -> None:
    """Plex tokens live in this file, so keep it owner-only where possible."""
    try:
        config.DB_PATH.chmod(0o600)
    except OSError:
        # Windows and some network filesystems do not support this. Not fatal.
        pass


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def open_readonly(path: str | Path) -> sqlite3.Connection:
    """Open a database strictly for reading.

    Opened through a URI with mode=ro, so SQLite rejects writes outright rather
    than the caller being trusted to behave. Used wherever a file must be
    inspected without any chance of modifying it.
    """
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return connect().execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    with _lock:
        return connect().execute(sql, params).fetchone()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """Run a write and return lastrowid."""
    with _lock:
        conn = connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.lastrowid or 0


def execute_rowcount(sql: str, params: Sequence[Any] = ()) -> int:
    """Run a write and return the number of rows affected.

    `execute()` returns lastrowid, which is only meaningful for an INSERT --
    a DELETE reports whatever id happened to be inserted last. Anything that
    counts what it changed must use this instead.
    """
    with _lock:
        conn = connect()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur.rowcount


def execute_many(sql: str, rows: Iterable[Sequence[Any]]) -> None:
    with _lock:
        conn = connect()
        conn.executemany(sql, rows)
        conn.commit()


def scalar(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    row = query_one(sql, params)
    return default if row is None else row[0]


def table_exists(name: str) -> bool:
    return (
        query_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        )
        is not None
    )


# ---------------------------------------------------------------------------
# settings table primitives (typed access lives in store.py)
# ---------------------------------------------------------------------------

def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
    if row is None or row["value"] is None:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )


def get_settings(*keys: str) -> dict[str, Any]:
    if keys:
        marks = ",".join("?" * len(keys))
        rows = query(f"SELECT key, value FROM settings WHERE key IN ({marks})", keys)
    else:
        rows = query("SELECT key, value FROM settings")
    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value"]) if row["value"] else None
        except (ValueError, TypeError):
            out[row["key"]] = row["value"]
    return out


def delete_setting(key: str) -> None:
    execute("DELETE FROM settings WHERE key = ?", (key,))


def now() -> int:
    return _now()
