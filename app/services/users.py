"""Discovering Plex users and getting a usable token for each of them.

User discovery needs THREE sources, because none is complete on its own:

  1. plex.tv home users      — Plex Home members, whose tokens can be minted
  2. plex.tv shared_servers  — friends this server is shared with. They are in
                               neither other list until they have watched
                               something, so someone you shared a library with
                               yesterday would otherwise be invisible.
  3. the server's /accounts  — everyone who has actually played something here

Ids are namespaced `tv:` / `pms:` because the two sources use overlapping small
integers that mean different things.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import store
from ..plex import account as plex_account
from ..plex.client import PlexAuthError, PlexError
from ..plex.types import PlexAccount
from .setup import _server, client_id, owner_token as stored_owner_token

log = logging.getLogger(__name__)

TV_PREFIX = "tv:"
PMS_PREFIX = "pms:"


async def refresh_users(owner_token: str = "") -> dict[str, Any]:
    """Rebuild the user list from plex.tv and from the server itself."""
    config = store.all_config()
    server_token = owner_token or stored_owner_token()
    account_token = str(config.get("plex_account_token") or "") or server_token

    discovered: list[PlexAccount] = []
    owner_plex_id: str | None = None

    # --- 1. Plex Home, via plex.tv ----------------------------------------
    if account_token:
        try:
            for acct in await plex_account.home_users(account_token, client_id()):
                acct.plex_id = f"{TV_PREFIX}{acct.plex_id}"
                discovered.append(acct)
                if acct.kind == "owner":
                    owner_plex_id = acct.plex_id
        except PlexError as exc:
            log.warning("Could not read Plex Home users from plex.tv: %s", exc)

    # --- 2. Friends this server is shared with ----------------------------
    # Checked before /accounts because it is the authoritative list of who has
    # access, and it sometimes carries a usable per-server token.
    shared_tokens: dict[str, str] = {}
    machine_id = str(config.get("plex_machine_id") or "")
    if account_token and machine_id:
        try:
            for acct in await plex_account.shared_users(
                account_token, client_id(), machine_id
            ):
                token = acct.access_token
                acct.plex_id = f"{TV_PREFIX}{acct.plex_id}"
                if token:
                    shared_tokens[acct.plex_id] = token
                discovered.append(acct)
        except PlexError as exc:
            log.warning("Could not list shared users: %s", exc)

    # --- 3. Accounts the server itself knows about ------------------------
    known_names = {
        (a.username or a.title).strip().lower()
        for a in discovered
        if (a.username or a.title)
    }
    known_names |= {a.title.strip().lower() for a in discovered if a.title}

    if server_token and config.get("plex_url"):
        server = _server()
        try:
            for entry in await server.accounts(server_token):
                account_id = str(entry.get("id") or "")
                name = str(entry.get("name") or "").strip()
                # id 0 is Plex's catch-all "everyone" row, not a real person.
                if not account_id or account_id == "0" or not name:
                    continue
                if name.lower() in known_names:
                    continue
                discovered.append(
                    PlexAccount(
                        plex_id=f"{PMS_PREFIX}{account_id}",
                        title=name,
                        username=name,
                        thumb=entry.get("thumb") or entry.get("defaultAvatarUrl"),
                        # Not in the Home, so no token can be minted for them --
                        # they have to paste one.
                        kind="shared",
                    )
                )
                known_names.add(name.lower())
        except PlexError as exc:
            log.warning("Could not read accounts from the server: %s", exc)
        finally:
            await server.aclose()

    # --- Persist ----------------------------------------------------------
    if not discovered and server_token:
        # No user list at all (plex.tv unreachable, /accounts unavailable).
        # Rather than leave the app unusable, record the owner from the token
        # already in hand.
        discovered.append(
            PlexAccount(plex_id=f"{TV_PREFIX}owner", title="Owner", kind="owner")
        )
        owner_plex_id = f"{TV_PREFIX}owner"

    for acct in discovered:
        if acct.plex_id == owner_plex_id:
            token = server_token
        else:
            # Only shared users ever bring their own here; Home users are linked
            # on demand from the Users page so a PIN can be asked for.
            token = shared_tokens.get(acct.plex_id)
        user_id = store.upsert_user(acct, token=token)
        if token:
            store.set_user_token(user_id, token)

    # If plex.tv never said who the owner is, fall back to the server's own
    # account 1, which is the owner in practice.
    if owner_plex_id is None and server_token:
        fallback = store.get_user_by_plex_id(f"{PMS_PREFIX}1")
        if fallback:
            store.set_user_kind(int(fallback["id"]), "owner")
            store.set_user_token(int(fallback["id"]), server_token)

    return summary()


def summary() -> dict[str, Any]:
    users = store.list_users()
    return {
        "total": len(users),
        "linked": sum(1 for u in users if u["token"]),
        "unlinked": sum(1 for u in users if not u["token"]),
        "shared_unlinked": sum(
            1 for u in users if u["kind"] == "shared" and not u["token"]
        ),
        "single_user": store.is_single_user(),
    }


async def link_home_user(user_id: int, pin: str = "") -> str:
    """Mint and store a token for a Plex Home member."""
    user = store.get_user(user_id)
    if not user:
        raise PlexError("That user no longer exists.")
    if user["kind"] == "shared":
        raise PlexError(
            f"{user['title']} has their own Plex account rather than being in "
            "your Plex Home, so Plex will not issue a token for them. They need "
            "to paste one in."
        )

    account_token = str(store.get_config("plex_account_token") or "")
    if not account_token:
        raise PlexError(
            "No plex.tv sign-in on record. Reconnect from Settings so home users "
            "can be linked."
        )

    plex_id = str(user["plex_id"])
    if not plex_id.startswith(TV_PREFIX):
        raise PlexError(
            f"{user['title']} was discovered from the server rather than plex.tv, "
            "so there is no Plex Home id to switch to. Refresh from Plex, or "
            "paste a token instead."
        )
    raw_id = plex_id[len(TV_PREFIX):]

    if raw_id == "owner":
        token = account_token
    else:
        token = await plex_account.switch_home_user(
            account_token, client_id(), raw_id, pin or None
        )

    machine_id = str(store.get_config("plex_machine_id") or "")
    if machine_id:
        token = await plex_account.server_token(token, client_id(), machine_id)

    await verify_and_store(user_id, token)
    return str(user["title"])


async def set_user_token(user_id: int, token: str) -> str:
    user = store.get_user(user_id)
    if not user:
        raise PlexError("That user no longer exists.")
    if not token.strip():
        raise PlexError("Paste a token first.")
    await verify_and_store(user_id, token.strip())
    return str(user["title"])


async def verify_and_store(user_id: int, token: str) -> None:
    """A token that cannot read the library is worse than no token — it would
    fail silently on every scheduled run — so prove it works before saving."""
    if not store.get_config("plex_url"):
        raise PlexError("Plex is not configured yet.")
    server = _server()
    try:
        await server.test_connection(token)
    except PlexAuthError as exc:
        raise PlexError(f"Plex rejected that token: {exc}") from exc
    finally:
        await server.aclose()
    store.set_user_token(user_id, token, "ok")


def set_enabled(user_id: int, enabled: bool) -> dict[str, Any]:
    user = store.get_user(user_id)
    if not user:
        raise PlexError("That user no longer exists.")
    store.set_user_enabled(user_id, enabled)
    return store.get_user(user_id) or {}


def delete(user_id: int) -> None:
    user = store.get_user(user_id)
    if not user:
        raise PlexError("That user no longer exists.")
    if user["kind"] == "owner":
        raise PlexError("The owner account cannot be removed.")
    store.delete_user(user_id)
