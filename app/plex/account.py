"""plex.tv account operations: sign-in, server discovery, and per-user tokens.

Two things worth knowing before reading this file:

1. Sign-in uses the PIN link flow, not an OAuth redirect. A NAS app usually is
   not reachable from the internet, so a redirect URL cannot be delivered back.
   A four-character code typed at plex.tv/link works from anywhere.

2. Plex stores watch state per token. The owner's token can only see and change
   the owner's own watched status — there is no impersonation parameter. Home
   and managed users can have a token minted here via the home-switch endpoint.
   Shared users (friends with their own Plex accounts, outside your Plex Home)
   cannot: Plex exposes no admin path to their tokens, so they have to paste one
   in. That is a Plex limitation, not something this code can route around.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

from .. import __version__
from ..config import APP_NAME
from .client import PlexAuthError, PlexError
from .types import PlexAccount, PlexResource

log = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv"
PINS_URL = f"{PLEX_TV}/api/v2/pins"
RESOURCES_URL = f"{PLEX_TV}/api/v2/resources"
HOME_USERS_V2 = f"{PLEX_TV}/api/v2/home/users"
HOME_USERS_V1 = f"{PLEX_TV}/api/home/users"
LINK_URL = "https://plex.tv/link"

# Hosts this module is allowed to fetch from. Anything that takes a stored URL
# and fetches it server-side needs this, or it is an open proxy into whatever
# the container can reach.
ALLOWED_HOSTS = ("plex.tv",)


def _host_allowed(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        return False
    return any(host == h or host.endswith(f".{h}") for h in ALLOWED_HOSTS)


def _headers(client_identifier: str, token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "X-Plex-Product": APP_NAME,
        "X-Plex-Version": __version__,
        "X-Plex-Client-Identifier": client_identifier,
        "X-Plex-Platform": "Docker",
        "X-Plex-Device": APP_NAME,
        "X-Plex-Device-Name": APP_NAME,
    }
    if token:
        headers["X-Plex-Token"] = token
    return headers


def _client(timeout: float = 20.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10.0), follow_redirects=True
    )


def _check(response: httpx.Response, what: str) -> None:
    if response.status_code in (401, 403):
        raise PlexAuthError(
            f"plex.tv rejected the request while {what} (HTTP {response.status_code})."
        )
    if response.status_code >= 400:
        raise PlexError(
            f"plex.tv returned HTTP {response.status_code} while {what}: "
            f"{response.text[:200]}"
        )


def _json_or_xml(response: httpx.Response) -> Any:
    """plex.tv is inconsistent about honouring Accept: application/json,
    especially on the older /api/ endpoints. Take whichever arrives."""
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return response.json()
        except ValueError:
            pass
    text = response.text.strip()
    if not text:
        return {}
    if text.startswith("<"):
        try:
            return ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise PlexError(f"Could not parse the plex.tv response: {exc}") from exc
    try:
        return response.json()
    except ValueError as exc:
        raise PlexError("Unexpected response format from plex.tv.") from exc


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------

async def create_pin(client_identifier: str) -> dict[str, Any]:
    """Start the link flow. Returns {id, code, link_url, auth_url}.

    `strong=false` yields the short four-character code that plex.tv/link
    accepts. Strong PINs are for the redirect-based OAuth flow and produce a
    long code the link page will not take.
    """
    async with _client() as client:
        response = await client.post(
            PINS_URL, headers=_headers(client_identifier), data={"strong": "false"}
        )
        _check(response, "requesting a link code")
        payload = response.json()

    pin_id = payload.get("id")
    code = payload.get("code")
    if not pin_id or not code:
        raise PlexError("plex.tv did not return a usable link code.")
    return {
        "id": str(pin_id),
        "code": str(code),
        "link_url": LINK_URL,
        # Pre-fills the code for anyone who would rather click than type.
        "auth_url": (
            f"https://app.plex.tv/auth#?clientID={client_identifier}"
            f"&code={code}"
            f"&context%5Bdevice%5D%5Bproduct%5D={APP_NAME.replace(' ', '+')}"
        ),
    }


async def poll_pin(pin_id: str, client_identifier: str) -> str | None:
    """Return the auth token once the user has entered the code, else None."""
    async with _client() as client:
        response = await client.get(
            f"{PINS_URL}/{pin_id}", headers=_headers(client_identifier)
        )
        if response.status_code == 404:
            raise PlexError("This link code has expired. Start the sign-in again.")
        _check(response, "checking the link code")
        payload = response.json()
    return payload.get("authToken") or None


# ---------------------------------------------------------------------------
# Server discovery
# ---------------------------------------------------------------------------

async def resources(token: str, client_identifier: str) -> list[PlexResource]:
    """Every Plex server this account can reach, with candidate addresses."""
    async with _client() as client:
        response = await client.get(
            RESOURCES_URL,
            headers=_headers(client_identifier, token),
            params={"includeHttps": 1, "includeRelay": 1},
        )
        _check(response, "listing your servers")
        payload = response.json()

    out: list[PlexResource] = []
    for entry in payload if isinstance(payload, list) else []:
        if "server" not in str(entry.get("provides") or ""):
            continue
        out.append(
            PlexResource(
                name=str(entry.get("name") or "Plex Media Server"),
                client_identifier=str(entry.get("clientIdentifier") or ""),
                connections=[
                    c for c in (entry.get("connections") or []) if isinstance(c, dict)
                ],
                owned=bool(entry.get("owned")),
                access_token=entry.get("accessToken"),
            )
        )
    return out


async def server_token(
    account_token: str, client_identifier: str, machine_identifier: str
) -> str:
    """Exchange a plex.tv account token for one scoped to a specific server.

    Account tokens usually work directly against a server the account can
    access, but the per-resource accessToken is what Plex itself uses and is the
    more reliable choice — particularly for managed users, whose account tokens
    are narrower.
    """
    try:
        for resource in await resources(account_token, client_identifier):
            if resource.client_identifier == machine_identifier and resource.access_token:
                return resource.access_token
    except PlexError as exc:
        log.debug("Could not resolve a server-scoped token: %s", exc)
    return account_token


# ---------------------------------------------------------------------------
# Home users
# ---------------------------------------------------------------------------

def _kind(is_admin: bool, is_restricted: bool) -> str:
    return "owner" if is_admin else ("managed" if is_restricted else "home")


def _account_from_json(entry: dict[str, Any]) -> PlexAccount | None:
    plex_id = entry.get("id")
    if plex_id is None:
        return None
    title = entry.get("title") or entry.get("username") or entry.get("friendlyName")
    return PlexAccount(
        plex_id=str(plex_id),
        uuid=entry.get("uuid"),
        title=str(title or f"User {plex_id}"),
        username=entry.get("username") or None,
        email=entry.get("email") or None,
        thumb=entry.get("thumb") or None,
        kind=_kind(bool(entry.get("admin")), bool(entry.get("restricted"))),
        protected=bool(entry.get("protected")),
    )


def _account_from_xml(element: ElementTree.Element) -> PlexAccount | None:
    attrib = element.attrib
    plex_id = attrib.get("id")
    if plex_id is None:
        return None

    def flag(name: str) -> bool:
        return attrib.get(name, "0") in ("1", "true", "True")

    return PlexAccount(
        plex_id=str(plex_id),
        uuid=attrib.get("uuid"),
        title=attrib.get("title") or attrib.get("username") or f"User {plex_id}",
        username=attrib.get("username") or None,
        email=attrib.get("email") or None,
        thumb=attrib.get("thumb") or None,
        kind=_kind(flag("admin"), flag("restricted")),
        protected=flag("protected"),
    )


async def home_users(token: str, client_identifier: str) -> list[PlexAccount]:
    """Everyone in the Plex Home, including the owner and managed profiles.

    Tries the JSON v2 endpoint first and falls back to the older XML one, which
    is what python-plexapi still uses and is the more dependable of the two.
    `/api/v2/friends` is gone (HTTP 410) — do not reach for it.
    """
    async with _client() as client:
        for url in (HOME_USERS_V2, HOME_USERS_V1):
            try:
                response = await client.get(
                    url, headers=_headers(client_identifier, token)
                )
                if response.status_code >= 400:
                    log.debug("%s returned HTTP %s", url, response.status_code)
                    continue
                payload = _json_or_xml(response)
            except (httpx.HTTPError, PlexError) as exc:
                log.debug("Home user lookup via %s failed: %s", url, exc)
                continue

            accounts: list[PlexAccount] = []
            if isinstance(payload, ElementTree.Element):
                for element in payload.iter("User"):
                    account = _account_from_xml(element)
                    if account:
                        accounts.append(account)
            else:
                entries = (
                    payload.get("users")
                    if isinstance(payload, dict)
                    else (payload if isinstance(payload, list) else None)
                )
                for entry in entries if isinstance(entries, list) else []:
                    if isinstance(entry, dict):
                        account = _account_from_json(entry)
                        if account:
                            accounts.append(account)

            if accounts:
                return accounts

    log.info(
        "plex.tv reported no Plex Home users; treating this as a single-user server."
    )
    return []


async def shared_users(
    account_token: str, client_identifier: str, machine_identifier: str
) -> list[PlexAccount]:
    """Friends this specific server is shared with.

    Needed because neither other source covers them: they are not Plex Home
    members, and the server's own /accounts only lists people who have actually
    watched something. Someone you shared the library with yesterday would
    otherwise be invisible.

    Plex has historically exposed an `accessToken` per shared server here. When
    it is present that user's watch state can be driven directly; when it is
    not, they fall back to pasting their own token. That fallback is the
    documented limitation and must keep working — the accessToken path is
    unverified against a real shared user.
    """
    if not machine_identifier:
        return []

    url = f"{PLEX_TV}/api/servers/{machine_identifier}/shared_servers"
    async with _client() as client:
        try:
            response = await client.get(
                url, headers=_headers(client_identifier, account_token)
            )
        except httpx.HTTPError as exc:
            log.debug("Could not list shared users: %s", exc)
            return []
        if response.status_code >= 400:
            log.debug("%s returned HTTP %s", url, response.status_code)
            return []
        payload = _json_or_xml(response)

    accounts: list[PlexAccount] = []

    def add(user_id: Any, title: Any, username: Any, email: Any, token: Any) -> None:
        if user_id is None:
            return
        accounts.append(
            PlexAccount(
                plex_id=str(user_id),
                title=str(title or username or email or f"User {user_id}"),
                username=str(username) if username else None,
                email=str(email) if email else None,
                kind="shared",
                access_token=str(token) if token else None,
            )
        )

    if isinstance(payload, ElementTree.Element):
        for element in payload.iter("SharedServer"):
            a = element.attrib
            add(
                a.get("userID") or a.get("id"),
                a.get("username") or a.get("friendlyName"),
                a.get("username"),
                a.get("email"),
                a.get("accessToken"),
            )
    elif isinstance(payload, dict):
        entries = payload.get("sharedServers") or payload.get("SharedServer") or []
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict):
                add(
                    entry.get("userID") or entry.get("id"),
                    entry.get("username") or entry.get("friendlyName"),
                    entry.get("username"),
                    entry.get("email"),
                    entry.get("accessToken"),
                )

    return accounts


async def switch_home_user(
    admin_token: str, client_identifier: str, user_id: str, pin: str | None = None
) -> str:
    """Mint an auth token for a Plex Home user.

    POST /api/home/users/{id}/switch, reading `authenticationToken` off the
    response. PIN-protected profiles need their PIN passed through.
    """
    params: dict[str, Any] = {}
    if pin:
        params["pin"] = pin

    async with _client() as client:
        response = await client.post(
            f"{HOME_USERS_V1}/{user_id}/switch",
            headers=_headers(client_identifier, admin_token),
            params=params,
        )
        if response.status_code in (401, 403):
            raise PlexAuthError(
                "Plex refused to switch to that user. If the profile is "
                "PIN-protected, enter its PIN; otherwise re-link the owner "
                "account."
            )
        _check(response, "switching to a home user")
        payload = _json_or_xml(response)

    token: str | None = None
    if isinstance(payload, ElementTree.Element):
        token = payload.attrib.get("authenticationToken") or payload.attrib.get(
            "authToken"
        )
        if not token:
            for element in payload.iter():
                token = element.attrib.get("authenticationToken") or element.attrib.get(
                    "authToken"
                )
                if token:
                    break
    elif isinstance(payload, dict):
        token = payload.get("authToken") or payload.get("authenticationToken")

    if not token:
        raise PlexError("Plex did not return a token for that user.")
    return str(token)


async def fetch_avatar(url: str, token: str, client_identifier: str) -> tuple[bytes, str]:
    """Fetch a user avatar from plex.tv.

    Home users' `thumb` is an absolute plex.tv URL, not a path on the media
    server, so it cannot go through the artwork proxy.

    The host allowlist is the point: this endpoint takes a URL from stored data
    and fetches it server-side, so without the check it would be an open proxy
    into whatever the container can reach.
    """
    if not _host_allowed(url):
        raise PlexError("Refusing to fetch an avatar from outside plex.tv.")

    async with _client() as client:
        response = await client.get(url, headers=_headers(client_identifier, token))
        _check(response, "fetching an avatar")
        return response.content, response.headers.get("content-type", "image/jpeg")
