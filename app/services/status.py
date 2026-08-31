"""The application status payload.

One place that answers "what is this thing doing right now", so the dashboard,
the health endpoint and any future UI all agree.
"""

from __future__ import annotations

from typing import Any

from .. import __version__, migrations, store
from ..config import APP_NAME
from ..engine import scheduler
from ..timeutil import format_duration, iso, relative, tz_name
from . import runs as runs_service


def status() -> dict[str, Any]:
    config = store.all_config()
    stats = store.stats()
    users = store.list_users()
    last = store.last_run()
    next_run = scheduler.next_run_time()

    unlinked = [u for u in users if u["enabled"] and not u["token"]]
    expired = [u for u in users if u["token_status"] == "invalid"]

    warnings: list[dict[str, str]] = []
    if not config.get("setup_complete"):
        warnings.append(
            {"level": "info", "message": "Plex is not connected yet. Finish setup."}
        )
    if not stats["rules_enabled"]:
        warnings.append(
            {
                "level": "warn",
                "message": "No enabled rules, so nothing will happen on a run.",
            }
        )
    if unlinked:
        warnings.append(
            {
                "level": "warn",
                "message": (
                    f"{len(unlinked)} user(s) have no working token, so their "
                    "watch history is left alone."
                ),
            }
        )
    if expired:
        warnings.append(
            {
                "level": "err",
                "message": f"{len(expired)} token(s) were rejected by Plex and need re-linking.",
            }
        )
    if not config.get("ui_password_hash"):
        warnings.append(
            {
                "level": "warn",
                "message": (
                    "No web UI password is set. Anyone who can reach this port "
                    "can change your Plex watch history."
                ),
            }
        )

    return {
        "app": {
            "name": APP_NAME,
            "version": __version__,
            "schema_version": migrations.SCHEMA_VERSION,
            "timezone": tz_name(),
        },
        "setup_complete": bool(config.get("setup_complete")),
        # The single most important flag in the app: while this is on, nothing
        # in Plex is ever modified.
        "safe_mode": bool(config.get("safe_mode")),
        "plex": {
            "server_name": config.get("plex_server_name") or "",
            "url": config.get("plex_url") or "",
            "machine_id": config.get("plex_machine_id") or "",
            "connected": bool(config.get("setup_complete") and config.get("plex_url")),
        },
        "schedule": {
            "enabled": bool(config.get("schedule_enabled")),
            "kind": config.get("schedule_kind"),
            "hours": config.get("schedule_hours"),
            "cron": config.get("schedule_cron"),
            "running": scheduler.is_running(),
            "next_run_at": next_run.isoformat(timespec="seconds") if next_run else None,
            "last_scheduled_run_at": iso(config.get("last_scheduled_run_at") or None),
        },
        "run": runs_service.current(),
        "last_run": _last_run(last),
        "stats": stats,
        "users": {
            "total": len(users),
            "linked": stats["users_linked"],
            "unlinked": len(unlinked),
            "expired": len(expired),
            "single_user": store.is_single_user(),
        },
        "migrated_from_v1": bool(config.get("migrated_from_v1")),
        "warnings": warnings,
    }


def _last_run(last: dict[str, Any] | None) -> dict[str, Any] | None:
    if not last:
        return None
    duration = None
    if last.get("finished_at") and last.get("started_at"):
        duration = int(last["finished_at"]) - int(last["started_at"])
    return {
        "id": last["id"],
        "uid": last["uid"],
        "mode": last["mode"],
        "trigger": last["trigger"],
        "status": last["status"],
        "scanned": last["scanned"],
        "matched": last["matched"],
        "applied": last["applied"],
        "failed": last["failed"],
        "skipped": last["skipped"],
        "started_at": iso(last["started_at"]),
        "finished_at": iso(last["finished_at"]),
        "relative": relative(last["started_at"]),
        "duration": format_duration(duration),
    }
