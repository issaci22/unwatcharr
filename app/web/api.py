"""The JSON API. This is the contract.

Every read and every mutation lives here; `pages.py` renders a disposable UI on
top of exactly these endpoints. Nothing in this module touches `store` or `db`
directly — services are the seam.

Errors are raised as HTTPException and rendered as `{"detail": "..."}` by the
handler in main.py.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response

from .. import logging_conf, notify, store
from ..config import APP_NAME
from ..engine import scheduler
from ..plex import account as plex_account
from ..plex.client import PlexError, PlexServer, first_reachable, is_safe_artwork_path
from ..services import migrate_v1
from ..services import rules as rules_service
from ..services import runs as runs_service
from ..services import setup as setup_service
from ..services import status as status_service
from ..services import users as users_service
from ..services.rules import RuleError
from ..services.runs import RunError
from . import security, viewmodels as vm

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def authed(request: Request) -> None:
    """Auth + origin check on every API route."""
    security.guard_origin(request)
    security.require(request)


def fail(message: str, status: int = 400) -> HTTPException:
    return HTTPException(status_code=status, detail=message)


# plex.tv account tokens waiting for the user to pick a server. Held in memory
# keyed by pin id, never handed to the browser -- the form posts the PIN ID
# back, which is a lookup key, not a secret.
_pending: dict[str, tuple[str, float]] = {}
_PENDING_TTL = 900


def _remember(pin_id: str, token: str) -> None:
    cutoff = time.time() - _PENDING_TTL
    for key, (_, created) in list(_pending.items()):
        if created < cutoff:
            _pending.pop(key, None)
    _pending[pin_id] = (token, time.time())


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@router.get("/status", dependencies=[Depends(authed)])
async def get_status() -> dict[str, Any]:
    return status_service.status()


@router.get("/schema", dependencies=[Depends(authed)])
async def get_schema() -> dict[str, Any]:
    """Vocabulary for building forms: age units, filter fields, reasons, …"""
    return vm.settings_schema()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

@router.get("/setup/v1-import", dependencies=[Depends(authed)])
async def v1_import_detect() -> dict[str, Any]:
    """Is there a Plex-Unwatcher v1 database worth offering to import?"""
    found = migrate_v1.detect()
    found["target_empty"] = migrate_v1.target_is_empty()
    return found


@router.post("/setup/v1-import", dependencies=[Depends(authed)])
async def v1_import_run(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    path = str(payload.get("path") or "").strip()
    if not path:
        found = migrate_v1.detect()
        if not found.get("found"):
            raise fail("No Plex-Unwatcher v1 database was found to import.", 404)
        path = str(found["path"])
    try:
        result = migrate_v1.import_v1(path, force=bool(payload.get("force")))
    except migrate_v1.MigrationError as exc:
        raise fail(str(exc)) from exc
    scheduler.reschedule()
    return result


@router.post("/setup/pin", dependencies=[Depends(authed)])
async def setup_pin() -> dict[str, Any]:
    store.ensure_bootstrap()
    try:
        return await plex_account.create_pin(setup_service.client_id())
    except PlexError as exc:
        raise fail(str(exc)) from exc


@router.get("/setup/pin/{pin_id}", dependencies=[Depends(authed)])
async def setup_pin_poll(pin_id: str) -> dict[str, Any]:
    client_id = setup_service.client_id()
    try:
        token = await plex_account.poll_pin(pin_id, client_id)
    except PlexError as exc:
        raise fail(str(exc)) from exc

    if not token:
        return {"authorised": False}

    _remember(pin_id, token)
    try:
        servers = await plex_account.resources(token, client_id)
    except PlexError as exc:
        raise fail(str(exc)) from exc

    return {
        "authorised": True,
        # The pin id, NOT the token. The token stays server-side.
        "pin_id": pin_id,
        "servers": [
            {
                "name": s.name,
                "machine_id": s.client_identifier,
                "owned": s.owned,
                "addresses": s.best_uris(),
            }
            for s in servers
        ],
    }


@router.post("/setup/server", dependencies=[Depends(authed)])
async def setup_server(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    pin_id = str(payload.get("pin_id") or "")
    machine_id = str(payload.get("machine_id") or "")
    entry = _pending.get(pin_id)
    if not entry:
        raise fail("That sign-in expired. Start again.")
    account_token = entry[0]

    client_id = setup_service.client_id()
    servers = await plex_account.resources(account_token, client_id)
    chosen = next((s for s in servers if s.client_identifier == machine_id), None)
    if chosen is None:
        raise fail("That server is no longer listed on your account.")

    server_token = chosen.access_token or account_token
    url = await first_reachable(chosen.best_uris(), server_token, client_id)
    if url is None:
        raise fail(
            f"None of the addresses Plex lists for {chosen.name} could be reached "
            "from this container. Try the manual option with a LAN address."
        )

    try:
        summary = await setup_service.connect(
            url,
            server_token,
            machine_id=machine_id,
            server_name=str(payload.get("name") or chosen.name),
            account_token=account_token,
        )
    except PlexError as exc:
        raise fail(str(exc)) from exc

    _pending.pop(pin_id, None)
    scheduler.reschedule()
    return summary


@router.post("/setup/manual", dependencies=[Depends(authed)])
async def setup_manual(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    url = str(payload.get("url") or "").strip()
    token = str(payload.get("token") or "").strip()
    if not url or not token:
        raise fail("Both a server address and a token are required.")
    store.ensure_bootstrap()
    try:
        summary = await setup_service.connect(url, token)
    except PlexError as exc:
        raise fail(str(exc)) from exc
    scheduler.reschedule()
    return summary


# ---------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------

@router.get("/libraries", dependencies=[Depends(authed)])
async def list_libraries(media_type: str = Query("")) -> dict[str, Any]:
    return {"libraries": vm.libraries(store.list_libraries(media_type or None))}


@router.post("/libraries/refresh", dependencies=[Depends(authed)])
async def refresh_libraries() -> dict[str, Any]:
    try:
        count = await setup_service.refresh_libraries()
    except PlexError as exc:
        raise fail(str(exc)) from exc
    return {"count": count, "libraries": vm.libraries(store.list_libraries())}


@router.get("/libraries/{library_id}/tags/{field}", dependencies=[Depends(authed)])
async def library_tags(library_id: int, field: str) -> dict[str, Any]:
    if field not in ("collection", "label", "genre"):
        raise fail("Tag field must be collection, label or genre.")
    try:
        return {"tags": await setup_service.section_tag_values(library_id, field)}
    except PlexError as exc:
        raise fail(str(exc)) from exc


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

@router.get("/rules", dependencies=[Depends(authed)])
async def list_rules() -> dict[str, Any]:
    rows = store.annotate_rules(store.list_rules())
    return {"rules": vm.rules(rows)}


@router.post("/rules", dependencies=[Depends(authed)])
async def create_rule(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        created = rules_service.create(payload)
    except RuleError as exc:
        raise fail(str(exc)) from exc
    return vm.rule(created, overrides=store.rule_overrides(int(created["id"])))


@router.get("/rules/{rule_id}", dependencies=[Depends(authed)])
async def get_rule(rule_id: int) -> dict[str, Any]:
    row = store.get_rule(rule_id)
    if row is None:
        raise fail("That rule no longer exists.", 404)
    return vm.rule(row, overrides=store.rule_overrides(rule_id))


@router.patch("/rules/{rule_id}", dependencies=[Depends(authed)])
async def update_rule(rule_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        updated = rules_service.update(rule_id, payload)
    except RuleError as exc:
        raise fail(str(exc)) from exc
    return vm.rule(updated, overrides=store.rule_overrides(rule_id))


@router.delete("/rules/{rule_id}", dependencies=[Depends(authed)])
async def delete_rule(rule_id: int) -> dict[str, Any]:
    try:
        rules_service.delete(rule_id)
    except RuleError as exc:
        raise fail(str(exc), 404) from exc
    return {"deleted": rule_id}


@router.post("/rules/{rule_id}/toggle", dependencies=[Depends(authed)])
async def toggle_rule(rule_id: int) -> dict[str, Any]:
    try:
        return vm.rule(rules_service.toggle(rule_id))
    except RuleError as exc:
        raise fail(str(exc), 404) from exc


@router.get("/rules/{rule_id}/thresholds", dependencies=[Depends(authed)])
async def rule_thresholds(rule_id: int) -> dict[str, Any]:
    try:
        return {"thresholds": rules_service.effective_thresholds(rule_id)}
    except RuleError as exc:
        raise fail(str(exc), 404) from exc


@router.post("/rules/{rule_id}/preview", dependencies=[Depends(authed)])
async def preview_rule(rule_id: int, user_id: int = Query(...)) -> dict[str, Any]:
    """Ephemeral: writes nothing, records no run, sends no notification."""
    try:
        return await runs_service.preview(rule_id, user_id)
    except (ValueError, PlexError) as exc:
        raise fail(str(exc)) from exc


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users", dependencies=[Depends(authed)])
async def list_users() -> dict[str, Any]:
    return {
        "users": vm.users(store.list_users()),
        "summary": users_service.summary(),
    }


@router.post("/users/refresh", dependencies=[Depends(authed)])
async def refresh_users() -> dict[str, Any]:
    try:
        summary = await users_service.refresh_users()
    except PlexError as exc:
        raise fail(str(exc)) from exc
    return {"users": vm.users(store.list_users()), "summary": summary}


@router.post("/users/{user_id}/link", dependencies=[Depends(authed)])
async def link_user(user_id: int, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    try:
        title = await users_service.link_home_user(user_id, str(payload.get("pin") or ""))
    except PlexError as exc:
        raise fail(str(exc)) from exc
    return {"linked": title, "users": vm.users(store.list_users())}


@router.post("/users/{user_id}/token", dependencies=[Depends(authed)])
async def set_user_token(user_id: int, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        title = await users_service.set_user_token(user_id, str(payload.get("token") or ""))
    except PlexError as exc:
        raise fail(str(exc)) from exc
    return {"linked": title, "users": vm.users(store.list_users())}


@router.post("/users/{user_id}/toggle", dependencies=[Depends(authed)])
async def toggle_user(user_id: int) -> dict[str, Any]:
    row = store.get_user(user_id)
    if row is None:
        raise fail("That user no longer exists.", 404)
    try:
        updated = users_service.set_enabled(user_id, not row["enabled"])
    except PlexError as exc:
        raise fail(str(exc)) from exc
    return vm.user(updated)


@router.delete("/users/{user_id}", dependencies=[Depends(authed)])
async def delete_user(user_id: int) -> dict[str, Any]:
    try:
        users_service.delete(user_id)
    except PlexError as exc:
        raise fail(str(exc)) from exc
    return {"deleted": user_id, "users": vm.users(store.list_users())}


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

@router.post("/runs", dependencies=[Depends(authed)])
async def start_run(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Starts in the background and returns immediately with a run id."""
    rule_ids = payload.get("rule_ids")
    user_ids = payload.get("user_ids")
    try:
        return await runs_service.start(
            mode=str(payload.get("mode") or "dry"),
            rule_ids=[int(r) for r in rule_ids] if rule_ids else None,
            user_ids=[int(u) for u in user_ids] if user_ids else None,
            trigger="manual",
        )
    except (RunError, ValueError) as exc:
        raise fail(str(exc)) from exc


@router.get("/runs/current", dependencies=[Depends(authed)])
async def current_run() -> dict[str, Any]:
    progress = runs_service.current()
    return progress or {"busy": False}


@router.post("/runs/cancel", dependencies=[Depends(authed)])
async def cancel_run() -> dict[str, Any]:
    return {"cancelling": runs_service.cancel()}


@router.get("/runs", dependencies=[Depends(authed)])
async def list_runs(
    limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0)
) -> dict[str, Any]:
    page = runs_service.list_runs(limit=limit, offset=offset)
    return {**page, "runs": vm.runs(page["runs"])}


@router.get("/runs/{run_id}", dependencies=[Depends(authed)])
async def get_run(run_id: int) -> dict[str, Any]:
    try:
        detail = runs_service.get_run(run_id)
    except RunError as exc:
        raise fail(str(exc), 404) from exc
    return {
        **vm.run(detail),
        "passes": [vm.run_pass(p) for p in detail["passes"]],
        "undoable": detail["undoable"],
    }


@router.get("/runs/{run_id}/items", dependencies=[Depends(authed)])
async def get_run_items(
    run_id: int, limit: int = Query(500, ge=1, le=2000)
) -> dict[str, Any]:
    try:
        return {"items": vm.actions(runs_service.run_items(run_id, limit=limit))}
    except RunError as exc:
        raise fail(str(exc), 404) from exc


@router.post("/runs/{run_id}/undo", dependencies=[Depends(authed)])
async def undo_run(run_id: int) -> dict[str, Any]:
    try:
        return await runs_service.undo_whole_run(run_id)
    except (RunError, PlexError) as exc:
        raise fail(str(exc)) from exc


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

@router.get("/history", dependencies=[Depends(authed)])
async def get_history(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    status: str = Query(""),
    user_id: int | None = Query(None),
    rule_id: int | None = Query(None),
    search: str = Query(""),
) -> dict[str, Any]:
    page = runs_service.history(
        limit=limit,
        offset=offset,
        status=status or None,
        user_id=user_id,
        rule_id=rule_id,
        search=search.strip() or None,
    )
    return {**page, "actions": vm.actions(page["actions"]), "caveat": runs_service.UNDO_CAVEAT}


@router.post("/actions/{action_id}/undo", dependencies=[Depends(authed)])
async def undo_one(action_id: int) -> dict[str, Any]:
    try:
        return await runs_service.undo_one(action_id)
    except (RunError, PlexError) as exc:
        raise fail(str(exc)) from exc


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

_INT_SETTINGS = {
    "request_delay_ms": (100, 0, 5000),
    "schedule_hours": (6, 1, 720),
    "history_keep_days": (365, 1, 3650),
    "dry_run_keep_days": (14, 1, 365),
}
_BOOL_SETTINGS = (
    "schedule_enabled",
    "catch_up_missed_runs",
    "server_side_filters",
    "plex_verify_ssl",
    "notify_enabled",
    "notify_on_dry_run",
    "notify_on_error_only",
    "log_to_file",
    "secure_cookies",
)
_STR_SETTINGS = {
    "schedule_kind": ("interval", "cron"),
    "notify_kind": ("webhook", "discord", "ntfy"),
}


@router.get("/settings", dependencies=[Depends(authed)])
async def get_settings() -> dict[str, Any]:
    # public_config() strips every SECRET_CONFIG_KEY.
    return {"settings": store.public_config(), "schema": vm.settings_schema()}


@router.post("/settings", dependencies=[Depends(authed)])
async def save_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Note safe_mode is deliberately NOT settable here — see /settings/safe-mode."""
    for key, (default, low, high) in _INT_SETTINGS.items():
        if key in payload:
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                raise fail(f"{key} must be a whole number.")
            store.set_config(key, max(low, min(high, value)))

    for key in _BOOL_SETTINGS:
        if key in payload:
            store.set_config(key, bool(payload[key]))

    for key, allowed in _STR_SETTINGS.items():
        if key in payload:
            value = str(payload[key])
            if value not in allowed:
                raise fail(f"{key} must be one of {', '.join(allowed)}.")
            store.set_config(key, value)

    if "schedule_cron" in payload:
        store.set_config("schedule_cron", str(payload["schedule_cron"]).strip() or "0 4 * * *")
    if "notify_url" in payload:
        store.set_config("notify_url", str(payload["notify_url"]).strip())

    # Any settings save must reschedule, or a new interval only takes effect
    # after a container restart.
    scheduler.reschedule()
    if "log_to_file" in payload:
        logging_conf.configure(to_file=bool(store.get_config("log_to_file")))

    next_run = scheduler.next_run_time()
    return {
        "settings": store.public_config(),
        "next_run_at": next_run.isoformat(timespec="seconds") if next_run else None,
    }


@router.post("/settings/safe-mode", dependencies=[Depends(authed)])
async def set_safe_mode(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Safe mode gets its own endpoint with an explicit confirmation.

    It is the difference between "this app can change my Plex history" and "it
    cannot", so it must never be a checkbox someone flips past in a long form.
    """
    enabled = bool(payload.get("enabled"))
    if not enabled and not payload.get("confirm"):
        raise fail(
            "Turning safe mode off lets runs really mark items unwatched in Plex. "
            "Send confirm: true once you have reviewed a preview.",
            409,
        )
    store.set_config("safe_mode", enabled)
    log.warning(
        "Safe mode turned %s. %s",
        "ON" if enabled else "OFF",
        "No run will change anything in Plex."
        if enabled
        else "Runs can now modify Plex watch state.",
    )
    return {"safe_mode": enabled}


@router.post("/settings/test-connection", dependencies=[Depends(authed)])
async def test_connection() -> dict[str, Any]:
    try:
        return {"message": await setup_service.test_connection()}
    except PlexError as exc:
        raise fail(str(exc)) from exc


@router.post("/settings/notify-test", dependencies=[Depends(authed)])
async def notify_test(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    kind = str(payload.get("notify_kind") or store.get_config("notify_kind") or "webhook")
    url = str(payload.get("notify_url") or store.get_config("notify_url") or "").strip()
    if not url:
        raise fail("Enter a notification URL first.")
    try:
        return {"message": await notify.send_test(kind, url)}
    except Exception as exc:  # noqa: BLE001 - surface whatever the endpoint said
        raise fail(f"Test failed: {exc}") from exc


@router.post("/settings/password", dependencies=[Depends(authed)])
async def set_password(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    password = str(payload.get("password") or "").strip()
    if not password:
        store.set_config("ui_password_hash", "")
        store.set_config("ui_password_salt", "")
        security.logout(request)
        log.warning("The web UI password was removed.")
        return {
            "password_set": False,
            "message": (
                "Password removed. Anyone who can reach this port can now change "
                "your Plex watch history."
            ),
        }
    if len(password) < 6:
        raise fail("Use at least 6 characters.")

    digest, salt = security.hash_password(password)
    store.set_config("ui_password_hash", digest)
    store.set_config("ui_password_salt", salt)
    # Re-issue this session against the new password; every other session dies.
    security.login(request)
    return {"password_set": True, "message": "Password updated."}


# ---------------------------------------------------------------------------
# Logs and artwork
# ---------------------------------------------------------------------------

@router.get("/logs", dependencies=[Depends(authed)])
async def get_logs(
    limit: int = Query(200, ge=1, le=2000),
    level: str = Query("DEBUG"),
    search: str = Query(""),
) -> dict[str, Any]:
    records = logging_conf.recent(limit=limit, min_level=level, search=search)
    return {"logs": [vm.log_record(r) for r in records], "levels":
            ["DEBUG", "INFO", "WARNING", "ERROR"]}


@router.get("/thumb", dependencies=[Depends(authed)])
async def thumb(path: str = Query(...)) -> Response:
    """Proxy artwork so a Plex token never has to reach the browser.

    Two shapes arrive here. Poster art is a path on the media server
    ("/library/metadata/123/thumb/1"); user avatars are absolute plex.tv URLs.
    They live on different hosts, need different clients, and each has its own
    allowlist — without those this endpoint is an open proxy into whatever the
    container can reach.
    """
    config = store.all_config()
    client_id = str(config.get("client_identifier") or "")
    owner = store.owner_user()
    token = (owner or {}).get("token") or config.get("plex_account_token")
    if not token:
        return Response(status_code=404)

    try:
        if path.startswith("/"):
            if not config.get("plex_url"):
                return Response(status_code=404)
            if not is_safe_artwork_path(path):
                raise fail("That is not an artwork path.", 400)
            server = PlexServer(
                str(config["plex_url"]),
                client_id,
                verify_ssl=bool(config.get("plex_verify_ssl")),
            )
            try:
                content, content_type = await server.thumb(path, str(token))
            finally:
                await server.aclose()
        else:
            # plex.tv avatar. fetch_avatar enforces the host allowlist.
            content, content_type = await plex_account.fetch_avatar(
                path, str(config.get("plex_account_token") or token), client_id
            )
    except PlexError:
        # Missing artwork should not draw attention to itself; the UI falls back
        # to an initial.
        return Response(status_code=404)

    return Response(
        content,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400"},
    )
