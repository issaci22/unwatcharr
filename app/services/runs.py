"""Starting runs, reading run history, and undoing changes.

Thin over `engine.runner` — its job is to keep the route layer away from both
the store and the run manager's internals.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from .. import store
from ..engine import runner as runner_engine
from ..engine.preview import preview_rule
from ..engine.runner import manager, undo_action, undo_run
from ..timeutil import format_duration

log = logging.getLogger(__name__)

MODES = ("dry", "apply")


class RunError(RuntimeError):
    """Something that stops a run being started or undone."""


async def start(
    *,
    mode: str = "dry",
    rule_ids: Sequence[int] | None = None,
    user_ids: Sequence[int] | None = None,
    trigger: str = "manual",
) -> dict[str, Any]:
    """Kick off a run in the background and return its identity immediately."""
    if mode not in MODES:
        raise RunError(f"Run mode must be one of {', '.join(MODES)}.")
    if manager.busy:
        raise RunError("A run is already in progress.")
    if not store.get_config("setup_complete"):
        raise RunError("Finish connecting to Plex first.")

    started = await manager.start(
        rule_ids=rule_ids, user_ids=user_ids, mode=mode, trigger=trigger
    )
    # Safe mode may have downgraded this; report what will actually happen
    # rather than what was asked for.
    started["safe_mode"] = bool(store.get_config("safe_mode"))
    started["effective_mode"] = "dry" if started["safe_mode"] else mode
    return started


def cancel() -> bool:
    return manager.request_cancel()


def current() -> dict[str, Any] | None:
    """Live progress, or None when nothing is running."""
    progress = manager.progress
    if progress is None:
        return None
    total = int(progress.get("total") or 0)
    done = int(progress.get("done") or 0)
    return {
        **progress,
        "busy": True,
        "percent": round(done / total * 100) if total else 0,
    }


async def preview(rule_id: int, user_id: int) -> dict[str, Any]:
    """Ephemeral: no run row, no Plex writes, no notification."""
    result = await preview_rule(rule_id, user_id)
    return result.as_dict()


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _decorate_run(run: dict[str, Any]) -> dict[str, Any]:
    duration = None
    if run.get("finished_at") and run.get("started_at"):
        duration = int(run["finished_at"]) - int(run["started_at"])
    return {
        **run,
        "duration_seconds": duration,
        "duration": format_duration(duration),
    }


def list_runs(limit: int = 25, offset: int = 0) -> dict[str, Any]:
    runs = [_decorate_run(r) for r in store.recent_runs(limit=limit, offset=offset)]
    return {"runs": runs, "total": store.run_count(), "limit": limit, "offset": offset}


def get_run(run_id: int) -> dict[str, Any]:
    run = store.get_run(run_id)
    if run is None:
        raise RunError("That run no longer exists.")
    passes = store.run_passes(run_id)
    for entry in passes:
        entry["skip_summary"] = _load_skip_summary(entry.get("skip_summary"))
    return {
        **_decorate_run(run),
        "passes": passes,
        "undoable": len(store.undoable_actions(run_id)),
    }


def _load_skip_summary(raw: Any) -> list[dict[str, Any]]:
    import json

    try:
        entries = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [
        {"reason": r, "count": n}
        for r, n in (e for e in entries if isinstance(e, (list, tuple)) and len(e) == 2)
    ]


def run_items(run_id: int, limit: int = 500) -> list[dict[str, Any]]:
    if store.get_run(run_id) is None:
        raise RunError("That run no longer exists.")
    return store.run_actions(run_id, limit=limit)


def pass_items(pass_id: int, limit: int = 500) -> list[dict[str, Any]]:
    return store.pass_actions(pass_id, limit=limit)


def history(
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    user_id: int | None = None,
    rule_id: int | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    actions, total = store.history(
        limit=limit,
        offset=offset,
        status=status,
        user_id=user_id,
        rule_id=rule_id,
        search=search,
    )
    return {"actions": actions, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

# Undo re-scrobbles. Plex records a FRESH play, so the original watch date and
# play count are gone for good. Every surface that offers Undo must say this.
UNDO_CAVEAT = (
    "Undo marks the item watched again. Plex records a new play, so the original "
    "watch date and play count cannot be restored."
)


async def undo_one(action_id: int) -> dict[str, Any]:
    try:
        await undo_action(action_id)
    except RuntimeError as exc:
        raise RunError(str(exc)) from exc
    return {"undone": 1, "failed": 0, "caveat": UNDO_CAVEAT}


async def undo_whole_run(run_id: int) -> dict[str, Any]:
    if store.get_run(run_id) is None:
        raise RunError("That run no longer exists.")
    undone, failed = await undo_run(run_id)
    return {"undone": undone, "failed": failed, "caveat": UNDO_CAVEAT}
