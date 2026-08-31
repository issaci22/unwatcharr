"""Shared test helpers.

This exists as its own module rather than living in a conftest because `tests/`
and `tests/e2e/` each have a conftest.py — importing `conftest` by module name
resolves to whichever one pytest loaded first, which is a genuinely confusing
way to lose an afternoon.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.engine.rules import EvalContext, Rule
from app.plex.types import MediaItem

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# A fixed clock. Every fixture timestamp is expressed relative to this, so the
# tests do not drift as the real date moves.
NOW = 1_760_000_000
DAY = 86400


def load(name: str) -> list[dict[str, Any]]:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return payload["MediaContainer"]["Metadata"]


def items(name: str) -> list[MediaItem]:
    return [MediaItem.from_json(entry) for entry in load(name)]


def movies() -> list[MediaItem]:
    return items("movies_section.json")


def shows() -> list[MediaItem]:
    return items("shows_section.json")


def episodes() -> list[MediaItem]:
    return items("episodes_all_leaves.json")


def by_title(collection: list[MediaItem]) -> dict[str, MediaItem]:
    return {item.title: item for item in collection}


def movie(**over: Any) -> MediaItem:
    """A plain watched-long-ago movie, which the default rule matches."""
    base: dict[str, Any] = {
        "rating_key": "m1",
        "type": "movie",
        "title": "A Movie",
        "view_count": 1,
        "last_viewed_at": NOW - 200 * DAY,
        "view_offset": 0,
    }
    base.update(over)
    return MediaItem(**base)


def episode(**over: Any) -> MediaItem:
    base: dict[str, Any] = {
        "rating_key": "e1",
        "type": "episode",
        "title": "An Episode",
        "view_count": 1,
        "last_viewed_at": NOW - 200 * DAY,
        "grandparent_rating_key": "s1",
        "grandparent_title": "A Show",
        "season": 1,
        "episode": 1,
    }
    base.update(over)
    return MediaItem(**base)


def rule(**over: Any) -> Rule:
    base: dict[str, Any] = {
        "name": "Test rule",
        "media_type": "movie",
        "age_value": 90,
        "age_unit": "days",
    }
    base.update(over)
    return Rule(**base)


def ctx(**over: Any) -> EvalContext:
    base: dict[str, Any] = {"now": NOW}
    base.update(over)
    return EvalContext(**base)
