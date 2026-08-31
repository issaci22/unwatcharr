"""Thin async client for the Plex Media Server HTTP API.

Deliberately not python-plexapi: that library builds a synchronous object graph
over XML and pulls in more than this app needs. Everything here is a handful of
JSON GETs, and keeping it small keeps the image small.

**The token is a per-call argument, not client state.** Watch state in Plex is
per-token, so a single run hits the same server with several different users'
tokens; sharing one connection pool across them is the whole point.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Iterable
from urllib.parse import urlparse

import httpx

from .. import __version__
from ..config import APP_NAME
from .types import Library, MediaItem

log = logging.getLogger(__name__)

LIBRARY_IDENTIFIER = "com.plexapp.plugins.library"
PAGE_SIZE = 500

# Path prefixes the artwork proxy is allowed to fetch from the media server.
# v1's proxy passed any path straight through, which let an authenticated user
# reach arbitrary PMS GET endpoints (including /:/unscrobble) through the app.
SAFE_ARTWORK_PREFIXES = ("/library/", "/photo/")


class PlexError(RuntimeError):
    """Any failure talking to Plex."""


class PlexAuthError(PlexError):
    """401/403 — the token is missing, expired, or not valid for this server."""


def normalise_base_url(url: str) -> str:
    """Accept what people actually paste: bare IPs, trailing slashes, no scheme."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise PlexError("Server URL is empty.")
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    if not parsed.hostname:
        raise PlexError(f"Could not parse a hostname out of {url!r}.")
    if not parsed.port and parsed.scheme == "http":
        # Almost everyone means :32400; save them the support ticket.
        url = f"{url}:32400"
    return url


def is_safe_artwork_path(path: str) -> bool:
    """Guard for the artwork proxy: a media-server path, not an arbitrary one."""
    if not path.startswith("/"):
        return False
    if "://" in path or path.startswith("//"):
        return False
    # Reject traversal before prefix matching, so "/library/../:/unscrobble"
    # cannot walk out of the allowlisted namespace.
    if ".." in path:
        return False
    return path.startswith(SAFE_ARTWORK_PREFIXES)


class PlexServer:
    def __init__(
        self,
        base_url: str,
        client_identifier: str,
        *,
        verify_ssl: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = normalise_base_url(base_url)
        self.client_identifier = client_identifier
        # Plex servers present a *.plex.direct certificate that will not validate
        # for a raw LAN IP, which is the normal way to reach it from another
        # container on the same box. Verification off is the pragmatic default
        # for a LAN-local hop; the README says so plainly and it is settable.
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=10.0),
            verify=verify_ssl,
            follow_redirects=True,
            headers=self.identity_headers(client_identifier),
        )

    @staticmethod
    def identity_headers(client_identifier: str) -> dict[str, str]:
        """Identify properly so the app appears as a named device in Plex's
        authorised-devices list rather than an anonymous mystery client."""
        return {
            "Accept": "application/json",
            "X-Plex-Product": APP_NAME,
            "X-Plex-Version": __version__,
            "X-Plex-Client-Identifier": client_identifier,
            "X-Plex-Platform": "Docker",
            "X-Plex-Device": APP_NAME,
            "X-Plex-Device-Name": APP_NAME,
        }

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "PlexServer":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _get(
        self,
        path: str,
        token: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        expect_json: bool = True,
    ) -> Any:
        request_headers = {"X-Plex-Token": token}
        if headers:
            request_headers.update(headers)
        try:
            response = await self._client.get(
                path, params=params or {}, headers=request_headers
            )
        except httpx.TimeoutException as exc:
            raise PlexError(
                f"Timed out talking to Plex at {self.base_url}{path}"
            ) from exc
        except httpx.HTTPError as exc:
            raise PlexError(f"Could not reach Plex at {self.base_url}: {exc}") from exc

        if response.status_code in (401, 403):
            raise PlexAuthError(
                f"Plex rejected the token (HTTP {response.status_code}). It may "
                "have expired, or may not have access to this server."
            )
        if response.status_code >= 400:
            raise PlexError(
                f"Plex returned HTTP {response.status_code} for {path}: "
                f"{response.text[:200]}"
            )

        if not expect_json:
            return response
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            # A misconfigured reverse proxy in front of Plex is the usual cause.
            raise PlexError(
                f"Plex returned a non-JSON response for {path}. Is "
                f"{self.base_url} really a Plex Media Server?"
            ) from exc

    @staticmethod
    def _container(payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            container = payload.get("MediaContainer")
            if isinstance(container, dict):
                return container
        return {}

    @classmethod
    def _metadata(cls, payload: Any) -> list[dict[str, Any]]:
        container = cls._container(payload)
        # Plex names the array after the thing it holds: Metadata for library
        # items, Directory for sections and tag lists, Video on some older
        # endpoints, Account for /accounts.
        for key in ("Metadata", "Directory", "Video", "Account"):
            entries = container.get(key)
            if isinstance(entries, list):
                return [e for e in entries if isinstance(e, dict)]
        return []

    # ------------------------------------------------------------------
    # Server info
    # ------------------------------------------------------------------

    async def identity(self, token: str = "") -> dict[str, Any]:
        """/identity needs no auth and is the cheapest reachability probe."""
        return self._container(await self._get("/identity", token))

    async def test_connection(self, token: str) -> dict[str, Any]:
        """Verify both that the server is reachable and that the token works."""
        identity = await self.identity(token)
        # /identity answers without a token, so probe something that does not.
        await self._get("/library/sections", token)
        return identity

    async def sections(self, token: str) -> list[Library]:
        payload = await self._get("/library/sections", token)
        return [Library.from_json(entry) for entry in self._metadata(payload)]

    async def accounts(self, token: str) -> list[dict[str, Any]]:
        """Accounts the *server* knows about. Includes shared users who have
        watched something here, which plex.tv's home-user list does not cover."""
        try:
            payload = await self._get("/accounts", token)
        except PlexError as exc:
            log.debug("Could not read /accounts: %s", exc)
            return []
        return self._metadata(payload)

    async def now_playing_keys(self, token: str) -> set[str]:
        """Rating keys currently being streamed by anyone on the server."""
        try:
            payload = await self._get("/status/sessions", token)
        except PlexError as exc:
            # Not being able to read sessions must not abort a run; it only means
            # the skip-now-playing gate cannot be enforced this pass.
            log.warning("Could not read /status/sessions: %s", exc)
            return set()
        return {
            str(entry["ratingKey"])
            for entry in self._metadata(payload)
            if entry.get("ratingKey") is not None
        }

    # ------------------------------------------------------------------
    # Library reads
    # ------------------------------------------------------------------

    async def iter_section_items(
        self,
        section_key: str,
        token: str,
        *,
        item_type: int,
        extra_params: dict[str, Any] | None = None,
        page_size: int = PAGE_SIZE,
    ) -> AsyncIterator[MediaItem]:
        """Page through a library section.

        Server-side filters passed in `extra_params` are an OPTIMISATION ONLY.
        Plex silently ignores a filter it does not recognise and hands back the
        whole library, so callers must still verify every field themselves —
        see engine/rules.py, which is where correctness actually lives.
        """
        start = 0
        total: int | None = None
        while True:
            params: dict[str, Any] = {"type": item_type}
            if extra_params:
                params.update(extra_params)
            payload = await self._get(
                f"/library/sections/{section_key}/all",
                token,
                params=params,
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(page_size),
                },
            )
            container = self._container(payload)
            if total is None:
                raw_total = container.get("totalSize", container.get("size", 0)) or 0
                try:
                    total = int(raw_total)
                except (TypeError, ValueError):
                    total = 0

            entries = self._metadata(payload)
            for entry in entries:
                yield MediaItem.from_json(entry)

            start += len(entries)
            if not entries or start >= total:
                return

    async def all_leaves(self, show_rating_key: str, token: str) -> list[MediaItem]:
        """Every episode of a show in one request."""
        payload = await self._get(
            f"/library/metadata/{show_rating_key}/allLeaves", token
        )
        return [MediaItem.from_json(entry) for entry in self._metadata(payload)]

    async def metadata(self, rating_key: str, token: str) -> MediaItem | None:
        payload = await self._get(f"/library/metadata/{rating_key}", token)
        entries = self._metadata(payload)
        return MediaItem.from_json(entries[0]) if entries else None

    async def section_tags(
        self, section_key: str, token: str, field: str
    ) -> list[dict[str, str]]:
        """Available values for a tag field: collection, label, genre.

        Populates the filter pickers. Returns [{"key": …, "title": …}].
        """
        try:
            payload = await self._get(f"/library/sections/{section_key}/{field}", token)
        except PlexError as exc:
            log.debug("No %s tags for section %s: %s", field, section_key, exc)
            return []
        out = []
        for entry in self._metadata(payload):
            title = entry.get("title")
            key = entry.get("key") or entry.get("ratingKey")
            if title and key is not None:
                out.append({"key": str(key), "title": str(title)})
        return out

    async def thumb(self, path: str, token: str) -> tuple[bytes, str]:
        """Fetch poster art so the browser never sees a Plex token.

        The allowlist is the point: this takes a stored path and fetches it
        server-side, so without the check it is a proxy into arbitrary PMS
        endpoints for anyone who can reach the UI.
        """
        if not is_safe_artwork_path(path):
            raise PlexError("Refusing to fetch that path from the media server.")
        response = await self._get(path, token, expect_json=False)
        return response.content, response.headers.get("content-type", "image/jpeg")

    # ------------------------------------------------------------------
    # Writes — each affects ONLY the account its token belongs to
    # ------------------------------------------------------------------

    async def unscrobble(self, rating_key: str, token: str) -> None:
        """Mark unwatched."""
        await self._get(
            "/:/unscrobble",
            token,
            params={"identifier": LIBRARY_IDENTIFIER, "key": rating_key},
            expect_json=False,
        )

    async def scrobble(self, rating_key: str, token: str) -> None:
        """Mark watched — this is what Undo uses.

        It cannot restore the original lastViewedAt or viewCount; Plex records a
        fresh play. Undo recovers watched status, not history.
        """
        await self._get(
            "/:/scrobble",
            token,
            params={"identifier": LIBRARY_IDENTIFIER, "key": rating_key},
            expect_json=False,
        )

    async def clear_progress(self, rating_key: str, token: str) -> None:
        """Zero the resume offset so no half-watched bar is left behind."""
        await self._get(
            "/:/progress",
            token,
            params={
                "identifier": LIBRARY_IDENTIFIER,
                "key": rating_key,
                "time": 0,
                "state": "stopped",
            },
            expect_json=False,
        )


async def first_reachable(
    uris: Iterable[str],
    token: str,
    client_identifier: str,
    *,
    timeout: float = 5.0,
    verify_ssl: bool = False,
) -> str | None:
    """Probe candidate URIs and return the first that answers /identity.

    Used by the setup wizard: plex.tv hands back several addresses per server
    and only some are routable from inside a container.
    """
    for uri in uris:
        server = PlexServer(
            uri, client_identifier, timeout=timeout, verify_ssl=verify_ssl
        )
        try:
            await server.identity(token)
            return server.base_url
        except PlexError as exc:
            log.debug("Candidate %s not reachable: %s", uri, exc)
        finally:
            await server.aclose()
        await asyncio.sleep(0)
    return None
