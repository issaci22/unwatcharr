"""Ephemeral rule preview: what would this rule do, and why.

v1's "Preview" was a full dry run that wrote `candidate` rows into the actions
table, which meant previewing polluted history and there was no way to see why
something was *skipped* — only what matched.

This writes nothing, records no run, and sends no notification. It returns both
sides of the decision, so the UI can make "would mark unwatched" and "left
alone, because X" equally visible.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .. import store
from ..plex.client import PlexServer
from ..timeutil import now as _now
from .collect import collect
from .rules import (
    Decision,
    EvalContext,
    collapse_to_series,
    describe_threshold,
    evaluate,
    summarise_skips,
)

log = logging.getLogger(__name__)

# A first pass over a large library can match thousands. The counts below are
# exact; only the per-item lists are capped.
ITEM_LIMIT = 300


@dataclass
class PreviewItem:
    rating_key: str
    title: str
    display_title: str
    item_type: str
    grandparent_title: str | None
    season: int | None
    episode: int | None
    year: int | None
    thumb: str | None
    last_viewed_at: int | None
    view_count: int
    matched: bool
    reason: str
    reason_text: str
    detail: str

    @classmethod
    def of(cls, decision: Decision) -> "PreviewItem":
        item = decision.item
        return cls(
            rating_key=item.rating_key,
            title=item.title,
            display_title=item.display_title,
            item_type=item.type,
            grandparent_title=item.grandparent_title,
            season=item.season,
            episode=item.episode,
            year=item.year,
            thumb=item.thumb,
            last_viewed_at=item.last_viewed_at,
            view_count=item.view_count,
            matched=decision.matched,
            reason=decision.reason,
            reason_text=decision.text,
            detail=decision.detail,
        )


@dataclass
class PreviewResult:
    rule_id: int
    rule_name: str
    user_id: int
    user_title: str
    media_type: str
    threshold: str
    libraries: list[str] = field(default_factory=list)
    scanned: int = 0
    matched: int = 0
    skipped: int = 0
    skip_summary: list[tuple[str, int]] = field(default_factory=list)
    would_change: list[PreviewItem] = field(default_factory=list)
    left_alone: list[PreviewItem] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "user_id": self.user_id,
            "user_title": self.user_title,
            "media_type": self.media_type,
            "threshold": self.threshold,
            "libraries": self.libraries,
            "scanned": self.scanned,
            "matched": self.matched,
            "skipped": self.skipped,
            "skip_summary": [{"reason": r, "count": n} for r, n in self.skip_summary],
            "would_change": [vars(i) for i in self.would_change],
            "left_alone": [vars(i) for i in self.left_alone],
            "truncated": self.truncated,
            "error": self.error,
        }


async def preview_rule(rule_id: int, user_id: int) -> PreviewResult:
    """Evaluate one rule for one user against live Plex. Read-only throughout."""
    rule_row = store.get_rule(rule_id)
    if rule_row is None:
        raise ValueError("That rule no longer exists.")
    user = store.get_user(user_id)
    if user is None:
        raise ValueError("That user no longer exists.")
    token = str(user.get("token") or "")
    if not token:
        raise ValueError(
            f"{user['title']} has no usable Plex token, so there is nothing to "
            "preview. Link them on the Users page."
        )

    config = store.all_config()
    if not config.get("plex_url"):
        raise ValueError("Plex is not configured yet. Finish setup first.")

    override = store.rule_overrides(rule_id).get(user_id)
    rule = store.build_rule(rule_row, override)

    result = PreviewResult(
        rule_id=rule_id,
        rule_name=rule.name,
        user_id=user_id,
        user_title=str(user["title"]),
        media_type=rule.media_type,
        threshold=describe_threshold(rule.age_value, rule.age_unit),
        libraries=[str(lib["title"]) for lib in rule_row.get("libraries", [])],
    )
    if not rule_row.get("libraries"):
        result.error = "This rule has no libraries selected."
        return result

    server = PlexServer(
        str(config["plex_url"]),
        str(config.get("client_identifier") or "unwatcharr"),
        verify_ssl=bool(config.get("plex_verify_ssl")),
    )
    try:
        ctx = EvalContext(now=_now())
        if rule.skip_now_playing:
            ctx.now_playing = await server.now_playing_keys(token)

        collected = await collect(
            server=server,
            token=token,
            rule=rule,
            ctx=ctx,
            libraries=rule_row["libraries"],
            server_side_filters=bool(config.get("server_side_filters")),
        )
        matched, skipped = evaluate(collected.items, rule, ctx)
        if rule.media_type == "show" and rule.tv_scope == "series":
            matched = collapse_to_series(matched, collected.shows)
    finally:
        await server.aclose()

    result.scanned = len(collected.items)
    result.matched = len(matched)
    result.skipped = len(skipped)
    result.skip_summary = summarise_skips(skipped)
    result.would_change = [PreviewItem.of(d) for d in matched[:ITEM_LIMIT]]
    result.left_alone = [PreviewItem.of(d) for d in skipped[:ITEM_LIMIT]]
    result.truncated = len(matched) > ITEM_LIMIT or len(skipped) > ITEM_LIMIT
    return result
