"""Page routes.

These routes render initial state only. Every mutation goes through the JSON
API in api.py via vanilla JS, so there is no second code path for reading data
and no server-side form handling to keep in sync with the contract.

The shell (app/web/templates/base.html) shows the connection, the schedule and
the safe-mode state on every page, so `context()` resolves the status payload
once per request for any signed-in visitor and hands it to the template. It is
a database-only read -- nothing here talks to Plex.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import security, viewmodels as vm
from .. import __version__, logging_conf, store
from ..config import APP_NAME
from ..services import runs as runs_service
from ..services import status as status_service
from ..timeutil import format_ts, relative

log = logging.getLogger(__name__)
router = APIRouter()

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


# ---------------------------------------------------------------------------
# Display filters
#
# The API hands out ISO-8601 strings already converted to the configured
# timezone (see docs/API.md, "Timestamps"). These render them the way a person
# reads a schedule -- "in 5h 20m", "Tomorrow 03:00" -- without a second source
# of truth for what time it is. Presentation only: no parsing decision here
# ever changes what the engine does.
# ---------------------------------------------------------------------------

def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def when(value: Any) -> str:
    """An absolute moment, dated only as far as it needs to be."""
    moment = _parse_iso(value)
    if moment is None:
        return "—"
    today = datetime.now(moment.tzinfo).date()
    days = (moment.date() - today).days
    clock = moment.strftime("%H:%M")
    if days == 0:
        return f"Today {clock}"
    if days == 1:
        return f"Tomorrow {clock}"
    if days == -1:
        return f"Yesterday {clock}"
    # %d, not %-d: the no-pad flag is glibc-only and this also runs on Windows.
    return moment.strftime("%d %b, %H:%M")


def until(value: Any) -> str:
    """How long until a scheduled moment: 'in 5h 20m'."""
    moment = _parse_iso(value)
    if moment is None:
        return "—"
    seconds = int((moment - datetime.now(moment.tzinfo)).total_seconds())
    if seconds <= 0:
        return "due now"
    if seconds < 3600:
        return f"in {max(1, seconds // 60)}m"
    if seconds < 86400:
        hours, minutes = divmod(seconds // 60, 60)
        return f"in {hours}h {minutes}m" if minutes else f"in {hours}h"
    days = seconds // 86400
    return f"in {days} day{'' if days == 1 else 's'}"


def threshold(rule: Any) -> str:
    """'30 days', '1 year' — the API's pre-rendered `threshold` never
    singularises, and "1 years" in a rule list reads as a bug."""
    if not isinstance(rule, dict):
        return str(rule or "")
    value, unit = rule.get("age_value"), rule.get("age_unit")
    if value is None or not unit:
        return str(rule.get("threshold") or "")
    if int(value) == 1 and str(unit).endswith("s"):
        unit = str(unit)[:-1]
    return f"{value} {unit}"


templates.env.filters["ts"] = format_ts
templates.env.filters["ago"] = relative
templates.env.filters["when"] = when
templates.env.filters["until"] = until
templates.env.filters["threshold"] = threshold


def context(request: Request, active: str = "", **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "request": request,
        "active": active,
        "app_name": APP_NAME,
        "version": __version__,
        "safe_mode": bool(store.get_config("safe_mode")),
        "password_set": security.password_is_set(),
        "single_user": store.is_single_user(),
        "setup_complete": bool(store.get_config("setup_complete")),
        "hide_nav": False,
        # The bare shell is a 460px column by default. The setup wizard is the
        # one page that needs it wider, and it is the shell that owns that
        # decision -- not a page overriding a width from inside.
        "bare_wide": False,
        # The shell renders the connection, the schedule and any live run from
        # this. It is only ever resolved for a signed-in visitor: the sign-in
        # page must not tell an unauthenticated caller which server this is
        # pointed at.
        "status": status_service.status() if security.is_authed(request) else None,
    }
    base.update(extra)
    return base


def page(request: Request, name: str, active: str = "", **extra: Any):
    return templates.TemplateResponse(request, name, context(request, active, **extra))


def _guard(request: Request):
    """Page routes redirect to /login; API routes raise 401 instead."""
    if security.is_authed(request):
        return None
    return RedirectResponse("/login", status_code=303)


def _needs_setup(request: Request):
    if not store.get_config("setup_complete"):
        return RedirectResponse("/setup", status_code=303)
    return None


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if not security.password_is_set():
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", "login", hide_nav=True, error=None)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, password: str = Form("")):
    if security.rate_limited(request):
        log.warning(
            "Too many failed sign-ins from %s",
            request.client.host if request.client else "?",
        )
        return templates.TemplateResponse(
            request,
            "login.html",
            context(
                request,
                "login",
                hide_nav=True,
                error="Too many attempts. Wait a few minutes and try again.",
            ),
            status_code=429,
        )

    config = store.all_config()
    if security.verify_password(
        password,
        str(config.get("ui_password_hash") or ""),
        str(config.get("ui_password_salt") or ""),
    ):
        security.clear_failures(request)
        security.login(request)
        return RedirectResponse("/", status_code=303)

    security.record_failure(request)
    log.warning(
        "Failed sign-in attempt from %s",
        request.client.host if request.client else "?",
    )
    return templates.TemplateResponse(
        request,
        "login.html",
        context(request, "login", hide_nav=True, error="That password is not right."),
        status_code=401,
    )


@router.post("/logout")
async def logout(request: Request):
    security.logout(request)
    return RedirectResponse("/login", status_code=303)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    if (redirect := _guard(request)) is not None:
        return redirect
    # The wizard renders all eight steps in one document and unhides one at a
    # time, so everything it can need is resolved here rather than per step.
    # `libraries` and `users` are the pre-connection state: on a first run both
    # are empty and the script fills them from the API the moment Plex answers.
    return page(
        request,
        "setup.html",
        "setup",
        hide_nav=not store.get_config("setup_complete"),
        bare_wide=True,
        schema=vm.settings_schema(),
        settings=store.public_config(),
        libraries=vm.libraries(store.list_libraries()),
        users=vm.users(store.list_users()),
    )


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if (redirect := _guard(request)) is not None:
        return redirect
    if (redirect := _needs_setup(request)) is not None:
        return redirect
    page_data = runs_service.list_runs(limit=10)
    # "What happened recently" is two different questions: which runs happened,
    # and what they actually changed. The dashboard answers both, and history()
    # already excludes dry-run candidates so nothing that did not happen can
    # appear in the second list.
    recent = runs_service.history(limit=6)
    return page(
        request,
        "dashboard.html",
        "dashboard",
        runs=vm.runs(page_data["runs"]),
        rules=vm.rules(store.list_rules()),
        actions=vm.actions(recent["actions"]),
    )


RULE_HISTORY_LOOKBACK = 5


def _last_pass_by_rule() -> dict[int, dict[str, Any]]:
    """The most recent run each rule actually took part in.

    A rule list that cannot say "and last time it matched 12 items" is asking
    the user to trust it blind. Only the newest run is not enough: a run that
    failed on connect, or one aimed at a single rule, would leave every other
    rule reading "not part of the last run" forever. So this walks back a few
    runs and keeps the first pass it finds per rule.

    A handful of indexed lookups per page load. No new SQL: `recent_runs` and
    `run_passes` already exist.
    """
    folded: dict[int, dict[str, Any]] = {}
    for run in store.recent_runs(limit=RULE_HISTORY_LOOKBACK):
        # One run fans out to a pass per user, so the totals for a rule are the
        # sum of its passes in that run — not whichever pass came back first.
        grouped: dict[int, dict[str, Any]] = {}
        for entry in store.run_passes(int(run["id"])):
            rule_id = entry.get("rule_id")
            if rule_id is None or int(rule_id) in folded:
                continue
            totals = grouped.setdefault(
                int(rule_id), {"scanned": 0, "matched": 0, "applied": 0, "users": 0}
            )
            totals["scanned"] += int(entry.get("scanned") or 0)
            totals["matched"] += int(entry.get("matched") or 0)
            totals["applied"] += int(entry.get("applied") or 0)
            totals["users"] += 1
        for rule_id, totals in grouped.items():
            folded[rule_id] = {
                "run_id": int(run["id"]),
                "mode": run["mode"],
                "status": run["status"],
                "relative": relative(run["started_at"]),
                **totals,
            }
    return folded


@router.get("/rules", response_class=HTMLResponse)
async def rules_page(request: Request):
    if (redirect := _guard(request)) is not None:
        return redirect
    if (redirect := _needs_setup(request)) is not None:
        return redirect
    return page(
        request,
        "rules.html",
        "rules",
        rules=vm.rules(store.annotate_rules(store.list_rules())),
        libraries=vm.libraries(store.list_libraries()),
        users=vm.users(store.list_users()),
        schema=vm.settings_schema(),
        last_pass=_last_pass_by_rule(),
    )


def _overrides_by_user(rules: list[dict[str, Any]]) -> dict[int, dict[int, dict[str, Any]]]:
    """`{user_id: {rule_id: override}}`.

    The store indexes the override table by rule, because that is how the rule
    editor reads it. The Users page asks the same question from the other end —
    "what does every rule do for *this person*" — so it is folded once here
    rather than re-derived per card. One indexed lookup per rule.
    """
    folded: dict[int, dict[int, dict[str, Any]]] = {}
    for rule in rules:
        rule_id = int(rule["id"])
        for user_id, override in store.rule_overrides(rule_id).items():
            folded.setdefault(int(user_id), {})[rule_id] = {
                "enabled": bool(override["enabled"]),
                "age_value": override["age_value"],
                "age_unit": override["age_unit"],
            }
    return folded


def _policy_counts(
    rules: list[dict[str, Any]],
    folded: dict[int, dict[int, dict[str, Any]]],
    user_ids: list[int],
) -> dict[int, dict[str, int]]:
    """How many enabled rules actually reach each user, and how many of those
    they have bent. A card that says "3 of 4 rules" is the honest headline; the
    matrix in the detail panel is where the reasoning lives."""
    active = [r for r in rules if r["enabled"]]
    counts: dict[int, dict[str, int]] = {}
    for user_id in user_ids:
        theirs = folded.get(int(user_id), {})
        applies = custom = excluded = 0
        for rule in active:
            override = theirs.get(int(rule["id"]))
            if override and not override["enabled"]:
                excluded += 1
                continue
            applies += 1
            if override and override["age_value"] is not None:
                custom += 1
        counts[int(user_id)] = {
            "applies": applies,
            "custom": custom,
            "excluded": excluded,
            "total": len(active),
        }
    return counts


def _user_activity(user_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Per user: how much has ever been changed for them, and the newest change.

    `history()` returns an exact `total` for whatever filter it was given, so a
    single row per user buys the real lifetime count without loading anybody's
    history into the page. Dry-run candidates are already excluded there, so
    this only ever counts changes that really happened.
    """
    out: dict[int, dict[str, Any]] = {}
    for user_id in user_ids:
        result = runs_service.history(limit=1, user_id=int(user_id))
        rows = result["actions"]
        out[int(user_id)] = {
            "total": int(result["total"]),
            # `relative` wants the epoch column, not the viewmodel's ISO string.
            "relative": relative(rows[0]["applied_at"]) if rows else "never",
            "last": vm.action(rows[0]) if rows else None,
        }
    return out


@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    if (redirect := _guard(request)) is not None:
        return redirect
    if (redirect := _needs_setup(request)) is not None:
        return redirect
    from ..services import users as users_service

    users = vm.users(store.list_users())
    rules = vm.rules(store.list_rules())
    overrides = _overrides_by_user(rules)
    user_ids = [int(u["id"]) for u in users]
    return page(
        request,
        "users.html",
        "users",
        users=users,
        summary=users_service.summary(),
        rules=rules,
        overrides=overrides,
        policy=_policy_counts(rules, overrides, user_ids),
        activity=_user_activity(user_ids),
        schema=vm.settings_schema(),
    )


HISTORY_RUN_LIMIT = 25
HISTORY_ACTION_LIMIT = 50
LOG_PAGE_LIMIT = 400


def _passes_by_run(runs: list[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    """`{run_id: [pass, ...]}` for the run rows' disclosure sections.

    A run row that only shows totals cannot answer the question the totals
    provoke — "so what did it leave alone, and why". The passes carry
    `skip_summary`, which is stored as JSON, so `runs_service.get_run` is the
    only thing that decodes it correctly; this reuses it rather than parsing
    the column a second time. One indexed lookup per listed run.
    """
    folded: dict[int, list[dict[str, Any]]] = {}
    for row in runs:
        run_id = int(row["id"])
        try:
            detail = runs_service.get_run(run_id)
        except runs_service.RunError:
            continue
        folded[run_id] = [vm.run_pass(p) for p in detail["passes"]]
    return folded


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request):
    if (redirect := _guard(request)) is not None:
        return redirect
    history = runs_service.history(limit=HISTORY_ACTION_LIMIT)
    runs_page = runs_service.list_runs(limit=HISTORY_RUN_LIMIT)
    runs = vm.runs(runs_page["runs"])
    return page(
        request,
        "history.html",
        "history",
        actions=vm.actions(history["actions"]),
        total=history["total"],
        action_limit=HISTORY_ACTION_LIMIT,
        runs=runs,
        run_total=runs_page["total"],
        passes=_passes_by_run(runs),
        users=vm.users(store.list_users()),
        rules=vm.rules(store.list_rules()),
        schema=vm.settings_schema(),
        caveat=runs_service.UNDO_CAVEAT,
    )


@router.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    if (redirect := _guard(request)) is not None:
        return redirect
    return page(
        request,
        "logs.html",
        "logs",
        logs=[vm.log_record(r) for r in logging_conf.recent(LOG_PAGE_LIMIT)],
        log_capacity=logging_conf.MAX_RECORDS,
        levels=["DEBUG", "INFO", "WARNING", "ERROR"],
    )


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if (redirect := _guard(request)) is not None:
        return redirect
    return page(
        request,
        "settings.html",
        "settings",
        settings=store.public_config(),
        schema=vm.settings_schema(),
    )
