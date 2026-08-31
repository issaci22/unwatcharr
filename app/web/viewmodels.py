"""Serialisers shared by the JSON API and the templates.

One set of builders, so the temporary UI and any future frontend see exactly the
same shape. This is the design-phase handoff: `docs/API.md` documents what these
produce.

THE RULE: nothing here may emit a Plex token. User rows carry `token_status`,
never `token`. A test asserts no token appears in any response body.
"""

from __future__ import annotations

from typing import Any, Iterable

from .. import store
from ..engine.rules import AGE_UNITS, FILTER_FIELDS, MEDIA_TYPES, REASON_TEXT, TV_SCOPES
from ..timeutil import format_duration, iso, relative


def user(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "plex_id": row["plex_id"],
        "title": row["title"],
        "username": row["username"],
        "kind": row["kind"],
        "protected": bool(row["protected"]),
        "enabled": bool(row["enabled"]),
        # Never the token itself -- only whether there is a working one.
        "linked": bool(row["token"]),
        "token_status": row["token_status"],
        "token_checked_at": iso(row["token_checked_at"]),
        # Can a token be minted for them, or must they paste one? Plex offers no
        # admin route to a shared user's token.
        "auto_linkable": row["kind"] in ("owner", "home", "managed"),
        "thumb": row["thumb"],
        "avatar_url": f"/api/thumb?path={row['thumb']}" if row["thumb"] else None,
    }


def users(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [user(r) for r in rows]


def library(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "section_key": row["section_key"],
        "title": row["title"],
        "type": row["type"],
    }


def libraries(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [library(r) for r in rows]


def rule(row: dict[str, Any], *, overrides: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    import json

    def load(raw: Any) -> list[dict[str, str]]:
        try:
            entries = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
        return [e for e in entries if isinstance(e, dict)]

    out = {
        "id": int(row["id"]),
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "media_type": row["media_type"],
        "age_value": row["age_value"],
        "age_unit": row["age_unit"],
        "threshold": f"{row['age_value']} {row['age_unit']}",
        "min_view_count": row["min_view_count"],
        "require_series_complete": bool(row["require_series_complete"]),
        "skip_in_progress": bool(row["skip_in_progress"]),
        "skip_now_playing": bool(row["skip_now_playing"]),
        "clear_progress": bool(row["clear_progress"]),
        "tv_scope": row["tv_scope"],
        "include_filters": load(row["include_filters"]),
        "exclude_filters": load(row["exclude_filters"]),
        "libraries": [
            {"id": l["id"], "title": l["title"], "type": l["type"]}
            for l in row.get("libraries", [])
        ],
        "custom_users": row.get("custom_users", 0),
        "excluded_users": row.get("excluded_users", 0),
        "updated_at": iso(row.get("updated_at")),
    }
    if overrides is not None:
        out["user_overrides"] = [
            {
                "user_id": uid,
                "enabled": bool(o["enabled"]),
                "age_value": o["age_value"],
                "age_unit": o["age_unit"],
            }
            for uid, o in sorted(overrides.items())
        ]
    return out


def rules(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [rule(r) for r in rows]


def action(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "run_id": row["run_id"],
        "pass_id": row["pass_id"],
        "rule_name": row.get("rule_name"),
        "user_title": row.get("user_title"),
        "user_id": row.get("user_id"),
        "rating_key": row["rating_key"],
        "item_type": row["item_type"],
        "title": row["title"],
        "grandparent_title": row["grandparent_title"],
        "season": row["season"],
        "episode": row["episode"],
        "year": row["year"],
        "display_title": _display(row),
        "thumb": row["thumb"],
        "poster_url": f"/api/thumb?path={row['thumb']}" if row["thumb"] else None,
        "last_viewed_at": iso(row["last_viewed_at"]),
        "last_viewed_relative": relative(row["last_viewed_at"]),
        "view_count_before": row["view_count_before"],
        "status": row["status"],
        "error": row["error"],
        "applied_at": iso(row["applied_at"]),
        "undone_at": iso(row["undone_at"]),
        "undoable": row["status"] == "applied",
    }


def actions(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [action(r) for r in rows]


def _display(row: dict[str, Any]) -> str:
    if row.get("grandparent_title"):
        code = ""
        if row.get("season") is not None and row.get("episode") is not None:
            code = f" S{int(row['season']):02d}E{int(row['episode']):02d}"
        return f"{row['grandparent_title']}{code} - {row['title']}"
    if row.get("year"):
        return f"{row['title']} ({row['year']})"
    return str(row["title"])


def run(row: dict[str, Any]) -> dict[str, Any]:
    duration = row.get("duration_seconds")
    if duration is None and row.get("finished_at") and row.get("started_at"):
        duration = int(row["finished_at"]) - int(row["started_at"])
    return {
        "id": int(row["id"]),
        "uid": row["uid"],
        "mode": row["mode"],
        "trigger": row["trigger"],
        "status": row["status"],
        "rules_processed": row["rules_processed"],
        "users_processed": row["users_processed"],
        "scanned": row["scanned"],
        "matched": row["matched"],
        "applied": row["applied"],
        "failed": row["failed"],
        "skipped": row["skipped"],
        "error": row["error"],
        "started_at": iso(row["started_at"]),
        "finished_at": iso(row["finished_at"]),
        "relative": relative(row["started_at"]),
        "duration": row.get("duration") or format_duration(duration),
        "duration_seconds": duration,
        # The distinction the UI must never blur.
        "changed_anything": row["mode"] == "apply" and int(row["applied"] or 0) > 0,
    }


def runs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [run(r) for r in rows]


def run_pass(row: dict[str, Any]) -> dict[str, Any]:
    duration = None
    if row.get("finished_at") and row.get("started_at"):
        duration = int(row["finished_at"]) - int(row["started_at"])
    return {
        "id": int(row["id"]),
        "run_id": row["run_id"],
        "rule_id": row["rule_id"],
        "rule_name": row["rule_name"],
        "user_id": row["user_id"],
        "user_title": row["user_title"],
        "status": row["status"],
        "scanned": row["scanned"],
        "matched": row["matched"],
        "applied": row["applied"],
        "failed": row["failed"],
        "skipped": row["skipped"],
        "skip_summary": row.get("skip_summary", []),
        "error": row["error"],
        "duration": format_duration(duration),
    }


def log_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": iso(record["ts"]),
        "level": record["level"],
        "logger": record["logger"],
        "message": record["message"],
    }


def settings_schema() -> dict[str, Any]:
    """The vocabulary a UI needs to render forms without hardcoding it."""
    return {
        "age_units": list(AGE_UNITS),
        "filter_fields": list(FILTER_FIELDS),
        "media_types": list(MEDIA_TYPES),
        "tv_scopes": list(TV_SCOPES),
        "notify_kinds": ["webhook", "discord", "ntfy"],
        "schedule_kinds": ["interval", "cron"],
        "skip_reasons": dict(REASON_TEXT),
        "user_kinds": list(store.USER_KINDS),
    }
