"""Connecting to a Plex server, and keeping the library list fresh.

Kept out of the route handlers so "connect to a server" means exactly the same
thing whether it was triggered by the setup wizard or by a reconnect later.
"""

from __future__ import annotations

import logging
from typing import Any

from .. import store
from ..plex.client import PlexError, PlexServer

log = logging.getLogger(__name__)


def client_id() -> str:
    return str(store.get_config("client_identifier") or "unwatcharr")


def _server(url: str | None = None) -> PlexServer:
    config = store.all_config()
    return PlexServer(
        str(url or config["plex_url"]),
        client_id(),
        verify_ssl=bool(config.get("plex_verify_ssl")),
    )


def owner_token() -> str:
    """The server-scoped token to use for read-only, server-wide operations.

    Prefers the owner's stored PMS token and falls back to the plex.tv account
    token, which usually works against a server the account owns.
    """
    owner = store.owner_user()
    if owner and owner.get("token"):
        return str(owner["token"])
    return str(store.get_config("plex_account_token") or "")


async def connect(
    url: str,
    token: str,
    *,
    machine_id: str = "",
    server_name: str = "",
    account_token: str = "",
) -> dict[str, Any]:
    """Verify a server/token pair, remember it, and load libraries and users."""
    from . import users as users_service

    server = _server(url)
    try:
        identity = await server.test_connection(token)
    finally:
        await server.aclose()

    resolved_machine_id = machine_id or str(identity.get("machineIdentifier") or "")
    store.set_config("plex_url", PlexServer(url, client_id()).base_url)
    store.set_config("plex_machine_id", resolved_machine_id)
    store.set_config(
        "plex_server_name",
        server_name or store.get_config("plex_server_name") or "Plex",
    )
    # A manually pasted token is usually an account token too; storing it lets
    # home-user linking work without a second sign-in.
    store.set_config("plex_account_token", account_token or token)

    library_count = await refresh_libraries(token=token)
    summary = await users_service.refresh_users(owner_token=token)
    store.set_config("setup_complete", True)

    log.info(
        "Connected to %s (%s): %d libraries, %d users",
        store.get_config("plex_server_name"),
        store.get_config("plex_url"),
        library_count,
        summary["total"],
    )
    return {
        "library_count": library_count,
        "server_name": store.get_config("plex_server_name"),
        "plex_url": store.get_config("plex_url"),
        **summary,
    }


async def refresh_libraries(token: str = "") -> int:
    config = store.all_config()
    token = token or owner_token()
    if not token:
        raise PlexError("No usable Plex token. Re-link the owner account.")
    if not config.get("plex_url"):
        raise PlexError("Plex is not configured yet.")

    server = _server()
    try:
        libraries = await server.sections(token)
    finally:
        await server.aclose()

    return store.sync_libraries(libraries)


async def test_connection() -> str:
    config = store.all_config()
    token = owner_token()
    if not config.get("plex_url") or not token:
        raise PlexError("Plex is not configured yet.")

    server = _server()
    try:
        identity = await server.test_connection(token)
        sections = await server.sections(token)
    finally:
        await server.aclose()

    supported = [s for s in sections if s.supported]
    return (
        f"Connected to Plex {identity.get('version', '?')} — "
        f"{len(supported)} video librar{'y' if len(supported) == 1 else 'ies'}."
    )


async def section_tag_values(library_id: int, field: str) -> list[dict[str, str]]:
    """Tag values available in a library, for the rule editor's filter pickers."""
    library = store.get_library(library_id)
    if library is None:
        raise PlexError("That library no longer exists.")
    token = owner_token()
    if not token:
        raise PlexError("No usable Plex token.")

    server = _server()
    try:
        return await server.section_tags(str(library["section_key"]), token, field)
    finally:
        await server.aclose()
