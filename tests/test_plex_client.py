"""The Plex client, driven through httpx.MockTransport.

No server, no network. The point of interest is the per-account invariant: a
token reads and writes only its own account's watch state, so every call has to
carry the caller's token rather than a shared one.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.plex import account as plex_account
from app.plex.client import (
    PlexAuthError,
    PlexError,
    PlexServer,
    first_reachable,
    is_safe_artwork_path,
    normalise_base_url,
)
from app.plex.types import TYPE_MOVIE, TYPE_SHOW, Library, MediaItem, PlexResource
from tests.support import FIXTURES, load

MOVIES = load("movies_section.json")
SHOWS = load("shows_section.json")
EPISODES = load("episodes_all_leaves.json")


class Recorder:
    """A fake PMS that remembers which token asked for what."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        token = request.headers.get("X-Plex-Token", "")
        self.calls.append((path, token, dict(request.url.params)))

        if path == "/identity":
            return httpx.Response(200, json={"MediaContainer": {
                "machineIdentifier": "mock-machine", "version": "1.41.0.0"}})
        if not token:
            return httpx.Response(401, text="no token")
        if path == "/library/sections":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [
                {"key": "1", "title": "Movies", "type": "movie"},
                {"key": "2", "title": "TV Shows", "type": "show"},
                {"key": "3", "title": "Music", "type": "artist"}]}})
        if path == "/accounts":
            return httpx.Response(200, json={"MediaContainer": {"Account": [
                {"id": "1", "name": "alice"}, {"id": "7", "name": "bob"}]}})
        if path == "/status/sessions":
            return httpx.Response(200, json={"MediaContainer": {"Metadata": [
                {"ratingKey": "101"}, {"ratingKey": "102"}]}})
        if path.endswith("/all"):
            kind = int(request.url.params.get("type", 1))
            start = int(request.headers.get("X-Plex-Container-Start", 0))
            size = int(request.headers.get("X-Plex-Container-Size", 500))
            items = MOVIES if kind == TYPE_MOVIE else SHOWS
            page = items[start:start + size]
            return httpx.Response(200, json={"MediaContainer": {
                "size": len(page), "totalSize": len(items), "Metadata": page}})
        if path.endswith("/allLeaves"):
            key = path.split("/")[3]
            eps = [e for e in EPISODES if e.get("grandparentRatingKey") == key]
            return httpx.Response(200, json={"MediaContainer": {"Metadata": eps}})
        if path == "/library/sections/1/collection":
            return httpx.Response(200, json={"MediaContainer": {"Directory": [
                {"key": "/library/sections/1/all?collection=9", "title": "Villeneuve"}]}})
        if path.startswith("/library/metadata/") and "thumb" in path:
            return httpx.Response(200, content=b"ART",
                                  headers={"content-type": "image/png"})
        if path in ("/:/unscrobble", "/:/scrobble", "/:/progress"):
            return httpx.Response(200, text="")
        return httpx.Response(404, text="nope")


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def server(recorder):
    s = PlexServer("http://mock:32400", "cid-test")
    s._client = httpx.AsyncClient(
        base_url=s.base_url,
        transport=httpx.MockTransport(recorder.handler),
        headers=PlexServer.identity_headers("cid-test"),
    )
    return s


# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "given,expected",
    [
        ("192.168.1.10", "http://192.168.1.10:32400"),
        ("http://plex:32400/", "http://plex:32400"),
        ("  http://plex:32400  ", "http://plex:32400"),
        ("https://x.plex.direct:32400", "https://x.plex.direct:32400"),
        ("http://plex", "http://plex:32400"),
    ],
)
def test_normalise_base_url(given, expected):
    assert normalise_base_url(given) == expected


def test_normalise_base_url_rejects_nonsense():
    with pytest.raises(PlexError):
        normalise_base_url("")


# ---------------------------------------------------------------------------
# The SSRF guards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "path", ["/library/metadata/1/thumb/123", "/library/sections/1/thumb", "/photo/:/transcode"]
)
def test_artwork_allowlist_permits_real_artwork(path):
    assert is_safe_artwork_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/:/unscrobble",
        "/status/sessions",
        "/library/../:/unscrobble",
        "//evil.example/x",
        "http://evil.example/x",
        "library/metadata/1/thumb",
        "/accounts",
    ],
)
def test_artwork_allowlist_blocks_everything_else(path):
    """v1 passed any path straight through, so /:/unscrobble was reachable
    through the thumb proxy."""
    assert not is_safe_artwork_path(path)


@pytest.mark.parametrize(
    "url", ["https://plex.tv/users/x/avatar", "https://sub.plex.tv/a"]
)
def test_plex_tv_host_allowlist_permits_plex(url):
    assert plex_account._host_allowed(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://plex.tv/a",              # not https
        "https://evil.example/a",
        "https://plex.tv.evil.example/a",  # suffix trick
        "https://notplex.tv/a",
        "https://169.254.169.254/latest/meta-data/",
    ],
)
def test_plex_tv_host_allowlist_blocks_everything_else(url):
    assert not plex_account._host_allowed(url)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def test_identity_needs_no_token(server):
    assert (await server.identity(""))["machineIdentifier"] == "mock-machine"


async def test_test_connection_rejects_a_bad_token(server):
    with pytest.raises(PlexAuthError):
        await server.test_connection("")


async def test_sections_marks_music_unsupported(server):
    sections = await server.sections("tok")
    assert [s.title for s in sections if s.supported] == ["Movies", "TV Shows"]


async def test_now_playing_keys(server):
    assert await server.now_playing_keys("tok") == {"101", "102"}


async def test_paging_returns_every_item_exactly_once(server):
    everything = [m async for m in server.iter_section_items("1", "tok", item_type=TYPE_MOVIE)]
    paged = [
        m async for m in server.iter_section_items("1", "tok", item_type=TYPE_MOVIE, page_size=2)
    ]
    assert len(everything) == len(MOVIES)
    assert [m.rating_key for m in paged] == [m.rating_key for m in everything]


async def test_all_leaves_returns_episodes(server):
    shows = [s async for s in server.iter_section_items("2", "tok", item_type=TYPE_SHOW)]
    eps = await server.all_leaves(shows[0].rating_key, "tok")
    assert eps and all(e.type == "episode" for e in eps)


async def test_section_tags(server):
    tags = await server.section_tags("1", "tok", "collection")
    assert tags[0]["title"] == "Villeneuve"


async def test_missing_tag_endpoint_degrades_to_empty(server):
    assert await server.section_tags("9", "tok", "collection") == []


async def test_thumb_refuses_a_non_artwork_path(server):
    with pytest.raises(PlexError, match="Refusing"):
        await server.thumb("/:/unscrobble", "tok")


async def test_thumb_serves_artwork(server):
    body, content_type = await server.thumb("/library/metadata/101/thumb", "tok")
    assert body == b"ART" and content_type == "image/png"


# ---------------------------------------------------------------------------
# Writes carry the caller's token
# ---------------------------------------------------------------------------

async def test_each_write_carries_its_own_token(server, recorder):
    """Plex watch state is per-account. Sharing one token across users would
    silently rewrite the wrong person's history."""
    await server.unscrobble("101", "tok-alice")
    await server.scrobble("102", "tok-bob")
    await server.clear_progress("103", "tok-alice")

    writes = [c for c in recorder.calls if c[0].startswith("/:/")]
    assert writes == [
        ("/:/unscrobble", "tok-alice", {"identifier": "com.plexapp.plugins.library", "key": "101"}),
        ("/:/scrobble", "tok-bob", {"identifier": "com.plexapp.plugins.library", "key": "102"}),
        ("/:/progress", "tok-alice",
         {"identifier": "com.plexapp.plugins.library", "key": "103", "time": "0", "state": "stopped"}),
    ]


async def test_identity_headers_name_the_app(server):
    headers = PlexServer.identity_headers("cid-test")
    assert headers["X-Plex-Product"] == "Unwatcharr"
    assert headers["X-Plex-Client-Identifier"] == "cid-test"


# ---------------------------------------------------------------------------
# Errors and tolerance
# ---------------------------------------------------------------------------

async def test_a_non_json_response_says_something_useful(recorder):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>reverse proxy login</html>",
                              headers={"content-type": "text/html"})

    server = PlexServer("http://mock:32400", "cid")
    server._client = httpx.AsyncClient(
        base_url=server.base_url, transport=httpx.MockTransport(handler))
    with pytest.raises(PlexError, match="really a Plex Media Server"):
        await server.identity("tok")


async def test_unreachable_server_is_reported(recorder):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    server = PlexServer("http://mock:32400", "cid")
    server._client = httpx.AsyncClient(
        base_url=server.base_url, transport=httpx.MockTransport(handler))
    with pytest.raises(PlexError, match="Could not reach Plex"):
        await server.identity("tok")


async def test_first_reachable_picks_the_first_that_answers(monkeypatch):
    reachable = {"http://good:32400"}

    def handler(request: httpx.Request) -> httpx.Response:
        base = f"{request.url.scheme}://{request.url.host}:{request.url.port}"
        if base in reachable:
            return httpx.Response(200, json={"MediaContainer": {"machineIdentifier": "m"}})
        raise httpx.ConnectError("refused")

    real_init = PlexServer.__init__

    def patched(self, base_url, cid, *, verify_ssl=False, timeout=30.0):
        real_init(self, base_url, cid, verify_ssl=verify_ssl, timeout=timeout)
        self._client = httpx.AsyncClient(
            base_url=self.base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(PlexServer, "__init__", patched)
    found = await first_reachable(
        ["http://bad:32400", "http://good:32400"], "tok", "cid")
    assert found == "http://good:32400"


async def test_first_reachable_returns_none_when_nothing_answers(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    real_init = PlexServer.__init__

    def patched(self, base_url, cid, *, verify_ssl=False, timeout=30.0):
        real_init(self, base_url, cid, verify_ssl=verify_ssl, timeout=timeout)
        self._client = httpx.AsyncClient(
            base_url=self.base_url, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(PlexServer, "__init__", patched)
    assert await first_reachable(["http://bad:32400"], "tok", "cid") is None


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

def test_media_item_tolerates_an_empty_payload():
    """Plex omits fields freely; one odd item must not abort a 4000-item run."""
    blank = MediaItem.from_json({})
    assert blank.view_count == 0
    assert blank.last_viewed_at is None
    assert blank.fully_watched_series is False
    assert blank.title == "Untitled"


def test_media_item_ignores_garbage_numbers():
    item = MediaItem.from_json({"viewCount": "many", "year": "", "lastViewedAt": None})
    assert item.view_count == 0 and item.year is None


def test_fully_watched_series_needs_both_counts():
    assert not MediaItem(rating_key="s", type="show", title="S",
                         leaf_count=None, viewed_leaf_count=3).fully_watched_series
    assert not MediaItem(rating_key="s", type="show", title="S",
                         leaf_count=0, viewed_leaf_count=0).fully_watched_series
    assert MediaItem(rating_key="s", type="show", title="S",
                     leaf_count=3, viewed_leaf_count=3).fully_watched_series


def test_resource_prefers_local_addresses():
    """Going out to the internet and back to reach a server on the same box
    would be daft, and plex.direct needs DNS the NAS may not have."""
    resource = PlexResource(
        name="Server", client_identifier="m",
        connections=[
            {"uri": "https://remote.plex.direct:32400", "local": False},
            {"uri": "http://192.168.1.10:32400", "local": True},
        ],
    )
    assert resource.best_uris()[0] == "http://192.168.1.10:32400"


def test_library_supported_types():
    assert Library("1", "Movies", "movie").supported
    assert Library("2", "Shows", "show").supported
    assert not Library("3", "Music", "artist").supported
    assert not Library("4", "Photos", "photo").supported
