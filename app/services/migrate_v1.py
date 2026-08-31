"""Importing a Plex-Unwatcher v1 installation.

The v1 database is opened READ-ONLY and is never written to, renamed, or moved.
An upgrade must be able to fail and leave the old installation exactly as it
was, so the user can go back to it.

What comes across, and what deliberately does not:

  settings      1:1, EXCEPT session_secret -- session secrets must not be shared
                between installations, so a fresh one is generated instead.
                Everyone gets logged out once; that is the correct trade.
  libraries     1:1 (matched later by section_key)
  plex_users    1:1, tokens included
  rules         v1 `rules.library_id` becomes one `rule_libraries` row, and
                `library_type` becomes `media_type`
  rule_users    1:1
  runs          v1 had one row per rule x user with a shared `batch` column.
                Rows are grouped by batch into one v2 `runs` row plus a
                `run_passes` row each.
  actions       1:1, remapped onto the new run/pass ids
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from .. import config as env, db, store
from ..timeutil import now as _now

log = logging.getLogger(__name__)

# Settings that must never be carried over.
SKIP_SETTINGS = frozenset({"session_secret"})

# v1 tables that must all be present for a file to be a v1 database.
REQUIRED_TABLES = ("settings", "plex_users", "libraries", "rules", "runs", "actions")


class MigrationError(RuntimeError):
    """A v1 import that cannot proceed."""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

def candidate_paths() -> list[Path]:
    paths: list[Path] = []
    for name in env.V1_DB_CANDIDATES:
        paths.append(env.CONFIG_DIR / name)
    seen: set[Path] = set()
    out = []
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


def detect() -> dict[str, Any]:
    """Look for an importable v1 database and summarise what is in it."""
    for path in candidate_paths():
        if not path.is_file():
            continue
        try:
            summary = inspect(path)
        except MigrationError as exc:
            log.debug("%s is not an importable v1 database: %s", path, exc)
            continue
        return {"found": True, **summary}
    return {"found": False, "searched": [str(p) for p in candidate_paths()]}


def inspect(path: str | Path) -> dict[str, Any]:
    """Read-only summary of a v1 database, for the wizard to show."""
    resolved = Path(path).expanduser()
    if not resolved.is_file():
        raise MigrationError(f"No file at {resolved}.")

    try:
        conn = db.open_readonly(resolved)
    except (sqlite3.Error, OSError, FileNotFoundError) as exc:
        raise MigrationError(f"Could not open {resolved}: {exc}") from exc

    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise MigrationError(
                f"This does not look like a Plex-Unwatcher database (missing "
                f"{', '.join(missing)})."
            )

        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        settings = _read_settings(conn)

        def count(table: str) -> int:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        return {
            "path": str(resolved),
            "schema_version": version,
            "server_name": settings.get("plex_server_name") or "",
            "plex_url": settings.get("plex_url") or "",
            "has_token": bool(settings.get("plex_account_token")),
            "counts": {
                "rules": count("rules"),
                "users": count("plex_users"),
                "libraries": count("libraries"),
                "runs": count("runs"),
                "actions": count("actions"),
                "overrides": count("rule_users") if "rule_users" in tables else 0,
            },
        }
    finally:
        conn.close()


def _read_settings(conn: sqlite3.Connection) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for row in conn.execute("SELECT key, value FROM settings"):
        try:
            out[row["key"]] = json.loads(row["value"]) if row["value"] else None
        except (ValueError, TypeError):
            out[row["key"]] = row["value"]
    return out


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def target_is_empty() -> bool:
    """True when nothing would be overwritten."""
    return (
        int(db.scalar("SELECT COUNT(*) FROM rules", default=0) or 0) == 0
        and int(db.scalar("SELECT COUNT(*) FROM plex_users", default=0) or 0) == 0
    )


# Everything an import populates, in dependency order. `force` clears these so
# a re-import overwrites rather than duplicating -- run uids are carried over
# from v1 batch ids, so a second pass would collide on runs.uid anyway.
_IMPORT_TABLES = (
    "actions",
    "run_passes",
    "runs",
    "rule_users",
    "rule_libraries",
    "rules",
    "plex_users",
    "libraries",
)


def clear_imported_data() -> None:
    """Wipe everything an import writes. Only ever called with force=True."""
    for table in _IMPORT_TABLES:
        db.execute(f"DELETE FROM {table}")
    log.warning("Cleared existing rules, users and history before a forced import.")


def import_v1(path: str | Path, *, force: bool = False) -> dict[str, Any]:
    """Copy a v1 installation into this one. Never writes to the v1 file."""
    summary = inspect(path)

    if not target_is_empty():
        if not force:
            raise MigrationError(
                "This installation already has rules or users. Importing would "
                "mix two configurations together. Start from an empty /config, "
                "or pass force to replace what is here."
            )
        # force means replace, not merge.
        clear_imported_data()

    conn = db.open_readonly(Path(path).expanduser())
    try:
        settings = _read_settings(conn)
        imported = {
            "settings": _import_settings(settings),
            "libraries": _import_libraries(conn),
            "users": _import_users(conn),
        }
        rule_map = _import_rules(conn)
        imported["rules"] = len(rule_map)
        imported["overrides"] = _import_overrides(conn, rule_map)
        run_counts = _import_runs(conn, rule_map)
        imported.update(run_counts)
    finally:
        conn.close()

    store.set_config("migrated_from_v1", True)
    store.set_config("migrated_from_v1_at", _now())
    store.register_known_secrets()

    log.info("Imported Plex-Unwatcher v1 from %s: %s", summary["path"], imported)
    return {"source": summary, "imported": imported}


def _import_settings(settings: dict[str, Any]) -> int:
    count = 0
    for key, value in settings.items():
        if key in SKIP_SETTINGS:
            continue
        # Only carry keys this version still understands, so a stale v1 key
        # cannot resurrect a setting that no longer exists.
        if key not in store.CONFIG_DEFAULTS:
            log.debug("Ignoring unknown v1 setting %r", key)
            continue
        store.set_config(key, value)
        count += 1
    return count


def _import_libraries(conn: sqlite3.Connection) -> int:
    count = 0
    for row in conn.execute("SELECT * FROM libraries"):
        db.execute(
            "INSERT INTO libraries (section_key, title, type, uuid, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(section_key) DO UPDATE SET "
            "title = excluded.title, type = excluded.type, uuid = excluded.uuid",
            (
                str(row["section_key"]),
                row["title"],
                row["type"],
                row["uuid"],
                int(row["updated_at"] or _now()),
            ),
        )
        count += 1
    return count


def _import_users(conn: sqlite3.Connection) -> int:
    count = 0
    for row in conn.execute("SELECT * FROM plex_users"):
        db.execute(
            "INSERT INTO plex_users (plex_id, uuid, title, username, email, thumb, "
            "kind, token, token_status, token_checked_at, protected, enabled, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(plex_id) DO UPDATE SET title = excluded.title, "
            "token = excluded.token, token_status = excluded.token_status",
            (
                row["plex_id"], row["uuid"], row["title"], row["username"],
                row["email"], row["thumb"], row["kind"], row["token"],
                row["token_status"], row["token_checked_at"],
                int(row["protected"] or 0), int(row["enabled"] or 1),
                int(row["created_at"] or _now()), int(row["updated_at"] or _now()),
            ),
        )
        count += 1
    return count


def _library_id_map(conn: sqlite3.Connection) -> dict[int, int]:
    """v1 library id -> v2 library id, matched on section_key."""
    new_by_key = {
        str(lib["section_key"]): int(lib["id"]) for lib in store.list_libraries()
    }
    out: dict[int, int] = {}
    for row in conn.execute("SELECT id, section_key FROM libraries"):
        new_id = new_by_key.get(str(row["section_key"]))
        if new_id is not None:
            out[int(row["id"])] = new_id
    return out


def _import_rules(conn: sqlite3.Connection) -> dict[int, int]:
    """Returns v1 rule id -> v2 rule id."""
    library_map = _library_id_map(conn)
    type_by_v1_library = {
        int(row["id"]): str(row["type"])
        for row in conn.execute("SELECT id, type FROM libraries")
    }

    mapping: dict[int, int] = {}
    order = store.next_sort_order()
    for row in conn.execute("SELECT * FROM rules ORDER BY id"):
        v1_library = int(row["library_id"])
        media_type = type_by_v1_library.get(v1_library, "movie")
        new_library = library_map.get(v1_library)
        if new_library is None:
            log.warning(
                "Rule %r pointed at a library that is not in this database; "
                "importing it with no libraries so it can be fixed by hand.",
                row["name"],
            )

        new_id = store.create_rule(
            name=str(row["name"]),
            enabled=int(row["enabled"] or 0),
            media_type=media_type,
            age_value=int(row["age_value"]),
            age_unit=str(row["age_unit"]),
            min_view_count=int(row["min_view_count"]),
            # Meaningless on a movie rule; v1 already stored 0 there.
            require_series_complete=(
                int(row["require_series_complete"]) if media_type == "show" else 0
            ),
            skip_in_progress=int(row["skip_in_progress"]),
            skip_now_playing=int(row["skip_now_playing"]),
            clear_progress=int(row["clear_progress"]),
            # v1 had no series scope; episode-level is what it did.
            tv_scope="episodes",
            include_filters=str(row["include_filters"] or "[]"),
            exclude_filters=str(row["exclude_filters"] or "[]"),
            sort_order=order,
            library_ids=[new_library] if new_library else [],
        )
        mapping[int(row["id"])] = new_id
        order += 1
    return mapping


def _user_id_map(conn: sqlite3.Connection) -> dict[int, int]:
    new_by_plex_id = {str(u["plex_id"]): int(u["id"]) for u in store.list_users()}
    out: dict[int, int] = {}
    for row in conn.execute("SELECT id, plex_id FROM plex_users"):
        new_id = new_by_plex_id.get(str(row["plex_id"]))
        if new_id is not None:
            out[int(row["id"])] = new_id
    return out


def _import_overrides(conn: sqlite3.Connection, rule_map: dict[int, int]) -> int:
    user_map = _user_id_map(conn)
    count = 0
    for row in conn.execute("SELECT * FROM rule_users"):
        rule_id = rule_map.get(int(row["rule_id"]))
        user_id = user_map.get(int(row["user_id"]))
        if rule_id is None or user_id is None:
            continue
        store.set_rule_override(
            rule_id,
            user_id,
            enabled=bool(row["enabled"]),
            age_value=row["age_value"],
            age_unit=row["age_unit"],
        )
        count += 1
    return count


def _import_runs(conn: sqlite3.Connection, rule_map: dict[int, int]) -> dict[str, int]:
    """v1 runs were per rule x user with a shared `batch`; regroup into
    runs + run_passes."""
    user_map = _user_id_map(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY id")]

    batches: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        # A run with no batch is its own batch, keyed on its id.
        key = str(row.get("batch") or f"run-{row['id']}")
        batches.setdefault(key, []).append(row)

    pass_map: dict[int, int] = {}
    runs_made = 0
    passes_made = 0

    for batch_key, group in batches.items():
        started = min(int(r["started_at"] or 0) for r in group)
        finished_values = [int(r["finished_at"]) for r in group if r["finished_at"]]
        mode = "apply" if any(r["mode"] == "apply" for r in group) else "dry"
        trigger = str(group[0].get("trigger") or "manual")
        status = "error" if any(r["status"] == "error" for r in group) else "ok"

        run_id = db.execute(
            "INSERT INTO runs (uid, mode, trigger, status, rules_processed, "
            "users_processed, scanned, matched, applied, failed, skipped, error, "
            "started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch_key[:12],
                mode,
                trigger,
                status,
                len({r["rule_id"] for r in group}),
                len({r["user_id"] for r in group}),
                sum(int(r["scanned"] or 0) for r in group),
                sum(int(r["matched"] or 0) for r in group),
                sum(int(r["applied"] or 0) for r in group),
                sum(int(r["failed"] or 0) for r in group),
                sum(int(r["skipped"] or 0) for r in group),
                next((r["error"] for r in group if r["error"]), None),
                started,
                max(finished_values) if finished_values else None,
            ),
        )
        runs_made += 1

        for row in group:
            pass_id = db.execute(
                "INSERT INTO run_passes (run_id, rule_id, rule_name, user_id, "
                "user_title, status, scanned, matched, applied, failed, skipped, "
                "skip_summary, error, started_at, finished_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,'[]',?,?,?)",
                (
                    run_id,
                    rule_map.get(int(row["rule_id"])) if row["rule_id"] else None,
                    str(row["rule_name"] or "Imported rule"),
                    user_map.get(int(row["user_id"])) if row["user_id"] else None,
                    str(row["user_title"] or "Imported user"),
                    str(row["status"] or "ok"),
                    int(row["scanned"] or 0),
                    int(row["matched"] or 0),
                    int(row["applied"] or 0),
                    int(row["failed"] or 0),
                    int(row["skipped"] or 0),
                    row["error"],
                    int(row["started_at"] or started),
                    row["finished_at"],
                ),
            )
            pass_map[int(row["id"])] = pass_id
            passes_made += 1

    actions = _import_actions(conn, pass_map, user_map, rule_map)
    return {"runs": runs_made, "run_passes": passes_made, "actions": actions}


def _import_actions(
    conn: sqlite3.Connection,
    pass_map: dict[int, int],
    user_map: dict[int, int],
    rule_map: dict[int, int],
) -> int:
    # v1 actions point at a v1 run row, which is now a run_pass.
    run_of_pass = {
        int(r["id"]): int(r["run_id"])
        for r in db.query("SELECT id, run_id FROM run_passes")
    }
    rule_of_pass = {
        int(r["id"]): (r["rule_id"], r["rule_name"])
        for r in db.query("SELECT id, rule_id, rule_name FROM run_passes")
    }

    count = 0
    for row in conn.execute("SELECT * FROM actions ORDER BY id"):
        pass_id = pass_map.get(int(row["run_id"])) if row["run_id"] else None
        if pass_id is None:
            continue
        rule_id, rule_name = rule_of_pass.get(pass_id, (None, None))
        db.execute(
            "INSERT INTO actions (run_id, pass_id, rule_id, rule_name, user_id, "
            "user_title, rating_key, item_type, title, grandparent_title, season, "
            "episode, thumb, year, last_viewed_at, view_count_before, "
            "view_offset_before, status, error, applied_at, undone_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?)",
            (
                run_of_pass.get(pass_id),
                pass_id,
                rule_id,
                rule_name,
                user_map.get(int(row["user_id"])) if row["user_id"] else None,
                row["user_title"],
                str(row["rating_key"]),
                row["item_type"],
                row["title"],
                row["grandparent_title"],
                row["season"],
                row["episode"],
                row["thumb"],
                row["year"],
                row["last_viewed_at"],
                row["view_count_before"],
                row["status"],
                row["error"],
                row["applied_at"],
                row["undone_at"],
            ),
        )
        count += 1
    return count
