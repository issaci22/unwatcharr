"""Repository layer: all SQL lives here, so services and the engine share it.

Nothing above this module writes SQL, and this module knows nothing about HTTP
or Plex. Config keys stored in the `settings` table are declared once in
CONFIG_DEFAULTS; anything absent from the table falls back to the value there.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from typing import Any, Sequence

from . import db, logging_conf
from .timeutil import now as _now

log = logging.getLogger(__name__)

# Every user-changeable setting, with its default. Adding one means adding it
# here plus wherever it is read; the settings API validates against this dict,
# so there is no third place to forget.
CONFIG_DEFAULTS: dict[str, Any] = {
    # --- Plex connection ---------------------------------------------------
    "plex_url": "",
    "plex_machine_id": "",
    "plex_server_name": "",
    "plex_verify_ssl": False,
    "client_identifier": "",
    "setup_complete": False,
    # The owner's plex.tv *account* token, kept apart from the server-scoped
    # tokens in plex_users.token. plex.tv operations (listing home users,
    # minting their tokens) need the account one; talking to the PMS wants the
    # scoped one. They are not interchangeable.
    "plex_account_token": "",
    # --- Scheduling --------------------------------------------------------
    "schedule_enabled": True,
    "schedule_kind": "interval",          # interval | cron
    "schedule_hours": 6,
    "schedule_cron": "0 4 * * *",
    "catch_up_missed_runs": True,
    "last_scheduled_run_at": 0,
    # --- Safety ------------------------------------------------------------
    "safe_mode": True,                    # forces every run to a dry run
    "request_delay_ms": 100,
    "server_side_filters": True,
    # --- Retention ---------------------------------------------------------
    "history_keep_days": 365,
    "dry_run_keep_days": 14,
    # --- Notifications -----------------------------------------------------
    "notify_enabled": False,
    "notify_kind": "webhook",             # webhook | discord | ntfy
    "notify_url": "",
    "notify_on_dry_run": False,
    "notify_on_error_only": False,
    # --- Logging -----------------------------------------------------------
    "log_to_file": False,
    # --- Web UI ------------------------------------------------------------
    "ui_password_hash": "",
    "ui_password_salt": "",
    "session_secret": "",
    "secure_cookies": False,
}

# Keys that must never leave the server. Referenced by the API serialisers and
# asserted on by a test.
SECRET_CONFIG_KEYS = frozenset(
    {"plex_account_token", "ui_password_hash", "ui_password_salt", "session_secret"}
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def get_config(key: str) -> Any:
    return db.get_setting(key, CONFIG_DEFAULTS.get(key))


def set_config(key: str, value: Any) -> None:
    db.set_setting(key, value)
    if key == "plex_account_token":
        logging_conf.register_secret(str(value or ""))


def all_config() -> dict[str, Any]:
    merged = dict(CONFIG_DEFAULTS)
    merged.update(db.get_settings())
    return merged


def public_config() -> dict[str, Any]:
    """Config with every secret removed -- the only shape allowed near a client."""
    config = all_config()
    for key in SECRET_CONFIG_KEYS:
        config.pop(key, None)
    config["password_set"] = bool(get_config("ui_password_hash"))
    return config


def ensure_bootstrap() -> None:
    """Fill in values that must exist before anything else can run."""
    from . import config as env

    if not get_config("client_identifier"):
        set_config("client_identifier", uuid.uuid4().hex)
    if not get_config("session_secret"):
        # Per install, never imported from a v1 database and never shared.
        set_config("session_secret", secrets.token_urlsafe(48))

    # Env pre-seeds apply on FIRST BOOT ONLY. After that the UI owns these, so a
    # stale compose file cannot silently clobber what was configured in the
    # browser.
    if not get_config("plex_url") and env.SEED_PLEX_URL:
        set_config("plex_url", env.SEED_PLEX_URL)
        log.info("Seeded the Plex URL from the PLEX_URL environment variable.")

    # v1 advertised PLEX_TOKEN in .env.example and docker-compose.yml and never
    # consumed it, so anyone who set it still got a wizard asking for a token.
    if not get_config("plex_account_token") and env.SEED_PLEX_TOKEN:
        set_config("plex_account_token", env.SEED_PLEX_TOKEN)
        log.info("Seeded the Plex token from the PLEX_TOKEN environment variable.")

    register_known_secrets()


def register_known_secrets() -> None:
    """Teach the log redactor every token currently on record."""
    logging_conf.register_secret(str(get_config("plex_account_token") or ""))
    if db.table_exists("plex_users"):
        for row in db.query("SELECT token FROM plex_users WHERE token IS NOT NULL"):
            logging_conf.register_secret(str(row["token"] or ""))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

USER_KINDS = ("owner", "home", "managed", "shared")


def list_users(include_disabled: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM plex_users"
    if not include_disabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY (kind = 'owner') DESC, title COLLATE NOCASE"
    return [dict(row) for row in db.query(sql)]


def get_user(user_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM plex_users WHERE id = ?", (user_id,))
    return dict(row) if row else None


def get_user_by_plex_id(plex_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM plex_users WHERE plex_id = ?", (str(plex_id),))
    return dict(row) if row else None


def owner_user() -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM plex_users WHERE kind = 'owner' LIMIT 1")
    return dict(row) if row else None


def runnable_users() -> list[dict[str, Any]]:
    """Users this app can actually act for: enabled and holding a working token."""
    return [
        dict(row)
        for row in db.query(
            "SELECT * FROM plex_users WHERE enabled = 1 AND token IS NOT NULL "
            "AND token != '' AND token_status != 'invalid' "
            "ORDER BY (kind = 'owner') DESC, title COLLATE NOCASE"
        )
    ]


def upsert_user(account: Any, *, token: str | None = None) -> int:
    """Insert or update from a PlexAccount, keeping any existing token unless a
    new one is supplied."""
    stamp = _now()
    existing = get_user_by_plex_id(account.plex_id)
    if existing:
        db.execute(
            "UPDATE plex_users SET uuid = ?, title = ?, username = ?, email = ?, "
            "thumb = ?, kind = ?, protected = ?, updated_at = ? WHERE id = ?",
            (
                account.uuid,
                account.title,
                account.username,
                account.email,
                account.thumb,
                account.kind,
                int(account.protected),
                stamp,
                existing["id"],
            ),
        )
        if token:
            set_user_token(int(existing["id"]), token)
        return int(existing["id"])

    user_id = db.execute(
        "INSERT INTO plex_users (plex_id, uuid, title, username, email, thumb, "
        "kind, token, token_status, protected, enabled, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)",
        (
            str(account.plex_id),
            account.uuid,
            account.title,
            account.username,
            account.email,
            account.thumb,
            account.kind,
            token,
            "ok" if token else "missing",
            int(account.protected),
            stamp,
            stamp,
        ),
    )
    logging_conf.register_secret(token)
    return user_id


def set_user_token(user_id: int, token: str | None, status: str = "ok") -> None:
    stamp = _now()
    db.execute(
        "UPDATE plex_users SET token = ?, token_status = ?, token_checked_at = ?, "
        "updated_at = ? WHERE id = ?",
        (token, status if token else "missing", stamp, stamp, user_id),
    )
    logging_conf.register_secret(token)


def set_user_token_status(user_id: int, status: str) -> None:
    stamp = _now()
    db.execute(
        "UPDATE plex_users SET token_status = ?, token_checked_at = ?, "
        "updated_at = ? WHERE id = ?",
        (status, stamp, stamp, user_id),
    )


def set_user_enabled(user_id: int, enabled: bool) -> None:
    db.execute(
        "UPDATE plex_users SET enabled = ?, updated_at = ? WHERE id = ?",
        (int(enabled), _now(), user_id),
    )


def set_user_kind(user_id: int, kind: str) -> None:
    db.execute(
        "UPDATE plex_users SET kind = ?, updated_at = ? WHERE id = ?",
        (kind, _now(), user_id),
    )


def delete_user(user_id: int) -> None:
    db.execute("DELETE FROM plex_users WHERE id = ?", (user_id,))


def user_count() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM plex_users", default=0) or 0)


def is_single_user() -> bool:
    """One account means per-user rules are noise. Drives UI collapsing only --
    never rule evaluation."""
    return user_count() <= 1


# ---------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------

def list_libraries(media_type: str | None = None) -> list[dict[str, Any]]:
    if media_type:
        rows = db.query(
            "SELECT * FROM libraries WHERE type = ? ORDER BY title COLLATE NOCASE",
            (media_type,),
        )
    else:
        rows = db.query("SELECT * FROM libraries ORDER BY title COLLATE NOCASE")
    return [dict(row) for row in rows]


def get_library(library_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM libraries WHERE id = ?", (library_id,))
    return dict(row) if row else None


def sync_libraries(libraries: Sequence[Any]) -> int:
    """Refresh the cached section list.

    Sections that vanished are dropped, which cascades through rule_libraries --
    a rule pointing only at a deleted library has nothing left to scan.
    """
    stamp = _now()
    seen: list[str] = []
    for library in libraries:
        if not library.supported:
            continue
        seen.append(library.section_key)
        db.execute(
            "INSERT INTO libraries (section_key, title, type, uuid, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(section_key) DO UPDATE SET "
            "title = excluded.title, type = excluded.type, uuid = excluded.uuid, "
            "updated_at = excluded.updated_at",
            (library.section_key, library.title, library.type, library.uuid, stamp),
        )
    if seen:
        marks = ",".join("?" * len(seen))
        db.execute(f"DELETE FROM libraries WHERE section_key NOT IN ({marks})", seen)
    return len(seen)


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

RULE_FIELDS = (
    "name",
    "enabled",
    "media_type",
    "age_value",
    "age_unit",
    "min_view_count",
    "require_series_complete",
    "skip_in_progress",
    "skip_now_playing",
    "clear_progress",
    "tv_scope",
    "include_filters",
    "exclude_filters",
    "sort_order",
)


def parse_filters(raw: str) -> list[Any]:
    """JSON filter blob -> engine Filter objects. Imported lazily so store stays
    free of an engine import at module scope."""
    from .engine.rules import Filter

    try:
        entries = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    out = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict) and entry.get("field") and entry.get("value"):
            out.append(Filter(str(entry["field"]), str(entry["value"])))
    return out


def dump_filters(filters: Sequence[Any]) -> str:
    return json.dumps([{"field": f.field, "value": f.value} for f in filters])


def _attach_libraries(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rules:
        return rules
    ids = [int(r["id"]) for r in rules]
    marks = ",".join("?" * len(ids))
    rows = db.query(
        "SELECT rl.rule_id, l.id, l.title, l.type, l.section_key "
        f"FROM rule_libraries rl JOIN libraries l ON l.id = rl.library_id "
        f"WHERE rl.rule_id IN ({marks}) ORDER BY l.title COLLATE NOCASE",
        ids,
    )
    by_rule: dict[int, list[dict[str, Any]]] = {i: [] for i in ids}
    for row in rows:
        by_rule[int(row["rule_id"])].append(
            {
                "id": int(row["id"]),
                "title": row["title"],
                "type": row["type"],
                "section_key": row["section_key"],
            }
        )
    for rule in rules:
        rule["libraries"] = by_rule.get(int(rule["id"]), [])
    return rules


def list_rules(enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM rules"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY sort_order, name COLLATE NOCASE"
    return _attach_libraries([dict(row) for row in db.query(sql)])


def get_rule(rule_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM rules WHERE id = ?", (rule_id,))
    if row is None:
        return None
    return _attach_libraries([dict(row)])[0]


def create_rule(**fields: Any) -> int:
    stamp = _now()
    library_ids = fields.pop("library_ids", []) or []
    columns = {k: v for k, v in fields.items() if k in RULE_FIELDS}
    columns.setdefault("name", "New rule")
    names = ", ".join(columns)
    marks = ", ".join("?" * len(columns))
    rule_id = db.execute(
        f"INSERT INTO rules ({names}, created_at, updated_at) "
        f"VALUES ({marks}, ?, ?)",
        (*columns.values(), stamp, stamp),
    )
    set_rule_libraries(rule_id, library_ids)
    return rule_id


def update_rule(rule_id: int, **fields: Any) -> None:
    library_ids = fields.pop("library_ids", None)
    updates = {k: v for k, v in fields.items() if k in RULE_FIELDS}
    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        db.execute(
            f"UPDATE rules SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), _now(), rule_id),
        )
    if library_ids is not None:
        set_rule_libraries(rule_id, library_ids)


def delete_rule(rule_id: int) -> None:
    db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))


def set_rule_libraries(rule_id: int, library_ids: Sequence[int]) -> None:
    db.execute("DELETE FROM rule_libraries WHERE rule_id = ?", (rule_id,))
    for library_id in dict.fromkeys(int(i) for i in library_ids):
        db.execute(
            "INSERT OR IGNORE INTO rule_libraries (rule_id, library_id) VALUES (?, ?)",
            (rule_id, library_id),
        )


def rule_library_ids(rule_id: int) -> list[int]:
    return [
        int(row["library_id"])
        for row in db.query(
            "SELECT library_id FROM rule_libraries WHERE rule_id = ?", (rule_id,)
        )
    ]


def next_sort_order() -> int:
    return int(db.scalar("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM rules", default=1) or 1)


# --- per-user overrides ----------------------------------------------------

def rule_overrides(rule_id: int) -> dict[int, dict[str, Any]]:
    rows = db.query("SELECT * FROM rule_users WHERE rule_id = ?", (rule_id,))
    return {int(row["user_id"]): dict(row) for row in rows}


def set_rule_override(
    rule_id: int,
    user_id: int,
    *,
    enabled: bool,
    age_value: int | None,
    age_unit: str | None,
) -> None:
    db.execute(
        "INSERT INTO rule_users (rule_id, user_id, enabled, age_value, age_unit) "
        "VALUES (?,?,?,?,?) ON CONFLICT(rule_id, user_id) DO UPDATE SET "
        "enabled = excluded.enabled, age_value = excluded.age_value, "
        "age_unit = excluded.age_unit",
        (rule_id, user_id, int(enabled), age_value, age_unit),
    )


def clear_rule_override(rule_id: int, user_id: int) -> None:
    db.execute(
        "DELETE FROM rule_users WHERE rule_id = ? AND user_id = ?", (rule_id, user_id)
    )


def annotate_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach override counts so a list can say, at a glance, that a rule does
    not treat everyone the same."""
    single = is_single_user()
    for rule in rules:
        if single:
            rule["custom_users"] = 0
            rule["excluded_users"] = 0
            continue
        overrides = rule_overrides(int(rule["id"]))
        rule["custom_users"] = sum(
            1 for o in overrides.values() if o["enabled"] and o["age_value"] is not None
        )
        rule["excluded_users"] = sum(1 for o in overrides.values() if not o["enabled"])
    return rules


def build_rule(row: dict[str, Any], override: dict[str, Any] | None = None) -> Any:
    """Turn a rule row (plus any per-user override) into the engine's Rule.

    THE single place an override is resolved into an effective threshold. The
    engine has no idea per-user rules exist -- keep it that way.
    """
    from .engine.rules import Rule

    age_value = row["age_value"]
    age_unit = row["age_unit"]
    if override:
        if override.get("age_value") is not None:
            age_value = override["age_value"]
        if override.get("age_unit"):
            age_unit = override["age_unit"]

    return Rule(
        id=int(row["id"]),
        name=str(row["name"]),
        media_type=str(row.get("media_type") or "movie"),
        age_value=int(age_value),
        age_unit=str(age_unit),
        min_view_count=int(row["min_view_count"]),
        require_series_complete=bool(row["require_series_complete"]),
        skip_in_progress=bool(row["skip_in_progress"]),
        skip_now_playing=bool(row["skip_now_playing"]),
        clear_progress=bool(row["clear_progress"]),
        tv_scope=str(row.get("tv_scope") or "episodes"),
        include_filters=parse_filters(row["include_filters"]),
        exclude_filters=parse_filters(row["exclude_filters"]),
    )


# ---------------------------------------------------------------------------
# Runs, passes and actions
# ---------------------------------------------------------------------------

def create_run(*, mode: str, trigger: str) -> tuple[int, str]:
    uid = uuid.uuid4().hex[:12]
    run_id = db.execute(
        "INSERT INTO runs (uid, mode, trigger, status, started_at) "
        "VALUES (?,?,?,'running',?)",
        (uid, mode, trigger, _now()),
    )
    return run_id, uid


RUN_TOTALS = ("scanned", "matched", "applied", "failed", "skipped")


def set_run_mode(run_id: int, mode: str) -> None:
    """Safe mode downgrades apply->dry after the run row already exists."""
    db.execute("UPDATE runs SET mode = ? WHERE id = ?", (mode, run_id))


def finish_run(run_id: int, **fields: Any) -> None:
    allowed = {"status", "error", "rules_processed", "users_processed", *RUN_TOTALS}
    updates = {k: v for k, v in fields.items() if k in allowed}
    assignments = ", ".join(f"{k} = ?" for k in updates)
    if assignments:
        assignments += ", "
    db.execute(
        f"UPDATE runs SET {assignments}finished_at = ? WHERE id = ?",
        (*updates.values(), _now(), run_id),
    )


def create_pass(
    *,
    run_id: int,
    rule_id: int | None,
    rule_name: str,
    user_id: int | None,
    user_title: str,
) -> int:
    return db.execute(
        "INSERT INTO run_passes (run_id, rule_id, rule_name, user_id, user_title, "
        "status, started_at) VALUES (?,?,?,?,?,'running',?)",
        (run_id, rule_id, rule_name, user_id, user_title, _now()),
    )


def finish_pass(pass_id: int, **fields: Any) -> None:
    allowed = {"status", "error", "skip_summary", *RUN_TOTALS}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if "skip_summary" in updates and not isinstance(updates["skip_summary"], str):
        updates["skip_summary"] = json.dumps(updates["skip_summary"])
    assignments = ", ".join(f"{k} = ?" for k in updates)
    if assignments:
        assignments += ", "
    db.execute(
        f"UPDATE run_passes SET {assignments}finished_at = ? WHERE id = ?",
        (*updates.values(), _now(), pass_id),
    )


def add_action(
    *,
    run_id: int,
    pass_id: int,
    rule_id: int | None,
    rule_name: str,
    user: dict[str, Any],
    item: Any,
    status: str = "candidate",
) -> int:
    return db.execute(
        "INSERT INTO actions (run_id, pass_id, rule_id, rule_name, user_id, "
        "user_title, rating_key, item_type, title, grandparent_title, season, "
        "episode, thumb, year, last_viewed_at, view_count_before, "
        "view_offset_before, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            pass_id,
            rule_id,
            rule_name,
            user.get("id"),
            user.get("title"),
            item.rating_key,
            item.type,
            item.title,
            item.grandparent_title,
            item.season,
            item.episode,
            item.thumb,
            item.year,
            item.last_viewed_at,
            item.view_count,
            item.view_offset,
            status,
        ),
    )


def mark_action(action_id: int, status: str, error: str | None = None) -> None:
    db.execute(
        "UPDATE actions SET status = ?, error = ?, applied_at = ? WHERE id = ?",
        (status, error, _now() if status == "applied" else None, action_id),
    )


def mark_action_undone(action_id: int) -> None:
    db.execute(
        "UPDATE actions SET status = 'undone', undone_at = ? WHERE id = ?",
        (_now(), action_id),
    )


def get_action(action_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM actions WHERE id = ?", (action_id,))
    return dict(row) if row else None


def get_run(run_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM runs WHERE id = ?", (run_id,))
    return dict(row) if row else None


def get_run_by_uid(uid: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM runs WHERE uid = ?", (uid,))
    return dict(row) if row else None


def recent_runs(limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.query(
            "SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    ]


def run_count() -> int:
    return int(db.scalar("SELECT COUNT(*) FROM runs", default=0) or 0)


def last_run() -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM runs ORDER BY started_at DESC, id DESC LIMIT 1")
    return dict(row) if row else None


def run_passes(run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.query(
            "SELECT * FROM run_passes WHERE run_id = ? ORDER BY id", (run_id,)
        )
    ]


def pass_actions(pass_id: int, limit: int = 500) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.query(
            "SELECT * FROM actions WHERE pass_id = ? ORDER BY "
            "grandparent_title COLLATE NOCASE, season, episode, "
            "title COLLATE NOCASE LIMIT ?",
            (pass_id, limit),
        )
    ]


def run_actions(run_id: int, limit: int = 500) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.query(
            "SELECT * FROM actions WHERE run_id = ? ORDER BY "
            "grandparent_title COLLATE NOCASE, season, episode, "
            "title COLLATE NOCASE LIMIT ?",
            (run_id, limit),
        )
    ]


def undoable_actions(run_id: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in db.query(
            "SELECT * FROM actions WHERE run_id = ? AND status = 'applied'", (run_id,)
        )
    ]


def history(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    user_id: int | None = None,
    rule_id: int | None = None,
    search: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Applied history. `candidate` rows are dry-run bookkeeping, not history."""
    where = ["a.status != 'candidate'"]
    params: list[Any] = []
    if status:
        where[0] = "a.status = ?"
        params.append(status)
    if user_id:
        where.append("a.user_id = ?")
        params.append(user_id)
    if rule_id:
        where.append("a.rule_id = ?")
        params.append(rule_id)
    if search:
        where.append("(a.title LIKE ? OR a.grandparent_title LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    clause = " AND ".join(where)

    total = int(
        db.scalar(f"SELECT COUNT(*) FROM actions a WHERE {clause}", params, default=0) or 0
    )
    rows = db.query(
        f"SELECT a.*, r.mode, r.trigger FROM actions a "
        f"LEFT JOIN runs r ON r.id = a.run_id WHERE {clause} "
        f"ORDER BY a.applied_at DESC, a.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [dict(row) for row in rows], total


def stats() -> dict[str, int]:
    day = 86400
    return {
        "applied_total": int(
            db.scalar("SELECT COUNT(*) FROM actions WHERE status = 'applied'", default=0) or 0
        ),
        "applied_7d": int(
            db.scalar(
                "SELECT COUNT(*) FROM actions WHERE status = 'applied' "
                "AND applied_at > ?",
                (_now() - 7 * day,),
                default=0,
            )
            or 0
        ),
        "applied_30d": int(
            db.scalar(
                "SELECT COUNT(*) FROM actions WHERE status = 'applied' "
                "AND applied_at > ?",
                (_now() - 30 * day,),
                default=0,
            )
            or 0
        ),
        "undone_total": int(
            db.scalar("SELECT COUNT(*) FROM actions WHERE status = 'undone'", default=0) or 0
        ),
        "rules_total": int(db.scalar("SELECT COUNT(*) FROM rules", default=0) or 0),
        "rules_enabled": int(
            db.scalar("SELECT COUNT(*) FROM rules WHERE enabled = 1", default=0) or 0
        ),
        "libraries": int(db.scalar("SELECT COUNT(*) FROM libraries", default=0) or 0),
        "users": user_count(),
        "users_linked": len(runnable_users()),
        "runs_total": run_count(),
    }


def prune_history(*, keep_days: int = 365, dry_keep_days: int = 14) -> dict[str, int]:
    """Dry-run bookkeeping piles up fast; applied history is worth keeping.

    Only the scheduled tick calls this, so a burst of manual runs never trims
    anything out from under someone reading the history page.
    """
    stamp = _now()
    dry_cutoff = stamp - max(1, dry_keep_days) * 86400
    keep_cutoff = stamp - max(1, keep_days) * 86400

    dry = db.execute_rowcount(
        "DELETE FROM runs WHERE finished_at IS NOT NULL AND finished_at < ? "
        "AND mode = 'dry'",
        (dry_cutoff,),
    )
    old = db.execute_rowcount(
        "DELETE FROM runs WHERE finished_at IS NOT NULL AND finished_at < ?",
        (keep_cutoff,),
    )
    return {"dry_runs_removed": dry, "old_runs_removed": old}
