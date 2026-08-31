"""Dataclasses for the slice of Plex's schema this app touches, plus tolerant
parsers.

Plex omits fields freely: `viewCount` is simply absent on something never
played, `lastViewedAt` does not exist until the first play, and episode payloads
carry a different shape from movie payloads. Every accessor here defaults rather
than raising, because one odd item in a 4000-item library must not abort a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Plex's numeric `type=` for /library/sections/N/all
TYPE_MOVIE = 1
TYPE_SHOW = 2
TYPE_SEASON = 3
TYPE_EPISODE = 4

# Library section types this app can act on. Music tracks do carry a viewCount,
# but "unwatching" an album is not a thing anyone has asked for, and photos have
# no watch state at all.
SUPPORTED_LIBRARY_TYPES = ("movie", "show")


def _int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _tags(payload: dict[str, Any], key: str) -> set[str]:
    """Plex returns tag lists as [{"tag": "Comedy"}, …] when it returns them at
    all. Lowercased so rule comparisons are case-insensitive."""
    raw = payload.get(key)
    if not isinstance(raw, list):
        return set()
    out: set[str] = set()
    for entry in raw:
        if isinstance(entry, dict):
            tag = entry.get("tag")
            if tag:
                out.add(str(tag).strip().lower())
    return out


@dataclass
class MediaItem:
    rating_key: str
    type: str                                # movie | episode | show | season
    title: str
    view_count: int = 0
    last_viewed_at: int | None = None
    view_offset: int = 0
    duration: int = 0
    year: int | None = None
    thumb: str | None = None
    grandparent_title: str | None = None     # series title, for episodes
    grandparent_rating_key: str | None = None
    parent_title: str | None = None          # season title, for episodes
    season: int | None = None
    episode: int | None = None
    added_at: int | None = None
    leaf_count: int | None = None            # shows/seasons only
    viewed_leaf_count: int | None = None
    genres: set[str] = field(default_factory=set)
    collections: set[str] = field(default_factory=set)
    labels: set[str] = field(default_factory=set)

    @property
    def watched(self) -> bool:
        return self.view_count > 0

    @property
    def in_progress(self) -> bool:
        return self.view_offset > 0

    @property
    def fully_watched_series(self) -> bool:
        """Only meaningful for `show` and `season` items."""
        if self.leaf_count is None or self.viewed_leaf_count is None:
            return False
        return self.leaf_count > 0 and self.viewed_leaf_count >= self.leaf_count

    @property
    def display_title(self) -> str:
        if self.type == "episode" and self.grandparent_title:
            code = ""
            if self.season is not None and self.episode is not None:
                code = f" S{self.season:02d}E{self.episode:02d}"
            return f"{self.grandparent_title}{code} - {self.title}"
        if self.year:
            return f"{self.title} ({self.year})"
        return self.title

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "MediaItem":
        return cls(
            rating_key=_str(payload.get("ratingKey")),
            type=_str(payload.get("type"), "unknown"),
            title=_str(payload.get("title"), "Untitled"),
            view_count=_int(payload.get("viewCount"), 0) or 0,
            last_viewed_at=_int(payload.get("lastViewedAt")),
            view_offset=_int(payload.get("viewOffset"), 0) or 0,
            duration=_int(payload.get("duration"), 0) or 0,
            year=_int(payload.get("year")),
            thumb=payload.get("thumb") or payload.get("grandparentThumb"),
            grandparent_title=payload.get("grandparentTitle"),
            grandparent_rating_key=(
                _str(payload["grandparentRatingKey"])
                if payload.get("grandparentRatingKey") is not None
                else None
            ),
            parent_title=payload.get("parentTitle"),
            season=_int(payload.get("parentIndex")),
            episode=_int(payload.get("index")),
            added_at=_int(payload.get("addedAt")),
            leaf_count=_int(payload.get("leafCount")),
            viewed_leaf_count=_int(payload.get("viewedLeafCount")),
            genres=_tags(payload, "Genre"),
            collections=_tags(payload, "Collection"),
            labels=_tags(payload, "Label"),
        )


@dataclass
class Library:
    section_key: str
    title: str
    type: str          # movie | show | artist | photo
    uuid: str | None = None

    @property
    def supported(self) -> bool:
        return self.type in SUPPORTED_LIBRARY_TYPES

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Library":
        return cls(
            section_key=_str(payload.get("key")),
            title=_str(payload.get("title"), "Untitled"),
            type=_str(payload.get("type"), "unknown"),
            uuid=payload.get("uuid"),
        )


@dataclass
class PlexAccount:
    """A user whose watch state this app may be able to touch.

    `kind` matters a lot: owner/home/managed users can have a token fetched
    automatically through plex.tv, shared users cannot (Plex exposes no way for
    an admin to obtain a friend's token) and must paste one in.
    """

    plex_id: str
    title: str
    uuid: str | None = None
    username: str | None = None
    email: str | None = None
    thumb: str | None = None
    kind: str = "home"          # owner | home | managed | shared
    protected: bool = False     # profile is PIN-locked
    # Only ever populated from the shared_servers listing, which sometimes hands
    # back a per-user token for this specific server.
    access_token: str | None = None

    @property
    def auto_linkable(self) -> bool:
        return self.kind in ("owner", "home", "managed")


@dataclass
class PlexResource:
    """A server returned by plex.tv /api/v2/resources."""

    name: str
    client_identifier: str
    connections: list[dict[str, Any]] = field(default_factory=list)
    owned: bool = True
    access_token: str | None = None

    def best_uris(self) -> list[str]:
        """Local addresses first — going out to the internet and back to reach a
        server on the same box would be daft, and plex.direct URIs need working
        DNS the NAS may not have."""
        local = [c for c in self.connections if c.get("local")]
        remote = [c for c in self.connections if not c.get("local")]
        uris: list[str] = []
        for group in (local, remote):
            for conn in group:
                uri = conn.get("uri")
                if uri and uri not in uris:
                    uris.append(str(uri))
        return uris
