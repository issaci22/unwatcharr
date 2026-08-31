"""Schema definition and forward-only migrations.

The rule that shapes this file: `CREATE TABLE IF NOT EXISTS` will never add a
column to an existing database. v1 built its schema from a big IF NOT EXISTS
script and kept `_migrate` as a separate, empty stub -- so a fresh install and
an upgrade took different code paths, and the upgrade path had never once run.

Here there is only one path. A brand new file is at `user_version = 0` and gets
the baseline by running migration 001, exactly as an existing database would.
Every fresh install therefore exercises the migration machinery.

Adding a schema change means: append a `Migration` with the next version number.
Never edit a migration that has shipped -- databases in the field have already
run it.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Migration:
    version: int
    description: str
    sql: str = ""
    run: Callable[[sqlite3.Connection], None] | None = None


_BASELINE = """
CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Plex accounts whose watch state this app may touch. `kind` decides how a
-- token is obtained: owner/home/managed can have one minted through plex.tv,
-- shared users cannot and must paste their own.
CREATE TABLE plex_users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    plex_id          TEXT    NOT NULL UNIQUE,
    uuid             TEXT,
    title            TEXT    NOT NULL,
    username         TEXT,
    email            TEXT,
    thumb            TEXT,
    kind             TEXT    NOT NULL DEFAULT 'home',
    token            TEXT,
    token_status     TEXT    NOT NULL DEFAULT 'missing',
    token_checked_at INTEGER,
    protected        INTEGER NOT NULL DEFAULT 0,
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);

CREATE TABLE libraries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    section_key TEXT    NOT NULL UNIQUE,
    title       TEXT    NOT NULL,
    type        TEXT    NOT NULL,
    uuid        TEXT,
    updated_at  INTEGER NOT NULL
);

-- A rule targets one media type and one or more libraries of that type. v1
-- welded a rule to a single library through rules.library_id; the join table
-- below replaces it.
CREATE TABLE rules (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT    NOT NULL,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    media_type              TEXT    NOT NULL DEFAULT 'movie',
    age_value               INTEGER NOT NULL DEFAULT 90,
    age_unit                TEXT    NOT NULL DEFAULT 'days',
    min_view_count          INTEGER NOT NULL DEFAULT 1,
    require_series_complete INTEGER NOT NULL DEFAULT 1,
    skip_in_progress        INTEGER NOT NULL DEFAULT 1,
    skip_now_playing        INTEGER NOT NULL DEFAULT 1,
    clear_progress          INTEGER NOT NULL DEFAULT 0,
    tv_scope                TEXT    NOT NULL DEFAULT 'episodes',
    include_filters         TEXT    NOT NULL DEFAULT '[]',
    exclude_filters         TEXT    NOT NULL DEFAULT '[]',
    sort_order              INTEGER NOT NULL DEFAULT 0,
    created_at              INTEGER NOT NULL,
    updated_at              INTEGER NOT NULL
);

CREATE TABLE rule_libraries (
    rule_id    INTEGER NOT NULL REFERENCES rules(id)     ON DELETE CASCADE,
    library_id INTEGER NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
    PRIMARY KEY (rule_id, library_id)
);

-- Per-user overrides. A NULL age_value means "inherit the rule default", so
-- changing the default later still moves everyone who never set their own.
CREATE TABLE rule_users (
    rule_id   INTEGER NOT NULL REFERENCES rules(id)      ON DELETE CASCADE,
    user_id   INTEGER NOT NULL REFERENCES plex_users(id) ON DELETE CASCADE,
    enabled   INTEGER NOT NULL DEFAULT 1,
    age_value INTEGER,
    age_unit  TEXT,
    PRIMARY KEY (rule_id, user_id)
);

-- One row per batch: a press of Run Now, or one scheduler tick.
CREATE TABLE runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uid             TEXT    NOT NULL UNIQUE,
    mode            TEXT    NOT NULL,
    trigger         TEXT    NOT NULL DEFAULT 'manual',
    status          TEXT    NOT NULL DEFAULT 'running',
    rules_processed INTEGER NOT NULL DEFAULT 0,
    users_processed INTEGER NOT NULL DEFAULT 0,
    scanned         INTEGER NOT NULL DEFAULT 0,
    matched         INTEGER NOT NULL DEFAULT 0,
    applied         INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    skipped         INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    started_at      INTEGER NOT NULL,
    finished_at     INTEGER
);

-- One row per rule x user inside a run. Watch state is per-token, so each user
-- is scanned separately with their own token and gets their own counts.
CREATE TABLE run_passes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id)       ON DELETE CASCADE,
    rule_id      INTEGER          REFERENCES rules(id)      ON DELETE SET NULL,
    rule_name    TEXT    NOT NULL,
    user_id      INTEGER          REFERENCES plex_users(id) ON DELETE SET NULL,
    user_title   TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'running',
    scanned      INTEGER NOT NULL DEFAULT 0,
    matched      INTEGER NOT NULL DEFAULT 0,
    applied      INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    skip_summary TEXT    NOT NULL DEFAULT '[]',
    error        TEXT,
    started_at   INTEGER NOT NULL,
    finished_at  INTEGER
);

-- The audit trail. A row is written BEFORE the Plex call, so an interrupted run
-- still leaves an undo trail.
CREATE TABLE actions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES runs(id)       ON DELETE CASCADE,
    pass_id            INTEGER          REFERENCES run_passes(id) ON DELETE CASCADE,
    rule_id            INTEGER,
    rule_name          TEXT,
    user_id            INTEGER,
    user_title         TEXT,
    rating_key         TEXT    NOT NULL,
    item_type          TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    grandparent_title  TEXT,
    season             INTEGER,
    episode            INTEGER,
    thumb              TEXT,
    year               INTEGER,
    last_viewed_at     INTEGER,
    view_count_before  INTEGER,
    view_offset_before INTEGER,
    status             TEXT    NOT NULL DEFAULT 'candidate',
    error              TEXT,
    applied_at         INTEGER,
    undone_at          INTEGER
);

CREATE INDEX idx_runs_started    ON runs(started_at DESC);
CREATE INDEX idx_passes_run      ON run_passes(run_id);
CREATE INDEX idx_actions_run     ON actions(run_id);
CREATE INDEX idx_actions_pass    ON actions(pass_id);
CREATE INDEX idx_actions_status  ON actions(status, applied_at DESC);
CREATE INDEX idx_actions_user    ON actions(user_id);
CREATE INDEX idx_rule_users_user ON rule_users(user_id);
CREATE INDEX idx_rule_libs_lib   ON rule_libraries(library_id);
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline schema", sql=_BASELINE),
)

SCHEMA_VERSION = max(m.version for m in MIGRATIONS)


def apply(conn: sqlite3.Connection) -> int:
    """Bring `conn` up to SCHEMA_VERSION. Returns the version it ended on."""
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])

    if current > SCHEMA_VERSION:
        # A downgrade. Refusing to touch it is safer than guessing which columns
        # a newer build added.
        log.warning(
            "This database is at schema version %s but this build of Unwatcharr "
            "expects %s. It was written by a newer version, so it will not be "
            "migrated.",
            current,
            SCHEMA_VERSION,
        )
        return current

    if current == SCHEMA_VERSION:
        return current

    fresh = current == 0
    for migration in MIGRATIONS:
        if migration.version <= current:
            continue
        log.info(
            "Applying database migration %03d: %s",
            migration.version,
            migration.description,
        )
        try:
            if migration.sql:
                conn.executescript(migration.sql)
            if migration.run is not None:
                migration.run(conn)
            # executescript() commits any open transaction, so the version bump
            # is a separate statement rather than part of the same one.
            conn.execute(f"PRAGMA user_version={migration.version}")
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            landed = int(conn.execute("PRAGMA user_version").fetchone()[0])
            raise RuntimeError(
                f"Database migration {migration.version:03d} "
                f"({migration.description}) failed: {exc}. The database is still "
                f"at version {landed}. Restore your /config backup before "
                "retrying."
            ) from exc

    log.info(
        "Database schema %s at version %s",
        "created" if fresh else "upgraded",
        SCHEMA_VERSION,
    )
    return SCHEMA_VERSION
