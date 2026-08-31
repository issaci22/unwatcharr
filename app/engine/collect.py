"""Fetching the candidate items a rule needs, for one user's token.

Split out of the runner so that preview and apply fetch identically — v1 had
this inlined in the runner, which is why its Preview had to be a full dry run
to see the same data.

Nothing here decides anything. It returns items plus the context the pure engine
needs (`series_complete`), and rules.py does the judging.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

from ..plex.client import PlexServer
from ..plex.types import TYPE_MOVIE, TYPE_SHOW, MediaItem
from .rules import EvalContext, Rule, build_series_index, cutoff_timestamp, inherit_show_tags

log = logging.getLogger(__name__)


@dataclass
class Collected:
    items: list[MediaItem] = field(default_factory=list)
    # Show items, kept so a `series`-scope rule can collapse episodes onto them
    # and so tag inheritance has something to read from.
    shows: list[MediaItem] = field(default_factory=list)
    libraries_scanned: int = 0


def prefilter(rule: Rule, ctx: EvalContext, enabled: bool) -> dict[str, Any]:
    """Server-side filters to shrink the payload.

    PURELY AN OPTIMISATION. Plex ignores filters it does not recognise and
    returns the whole library, so rules.py re-checks every field regardless. The
    worst case if Plex misreads one is that too few candidates are fetched and
    the run under-delivers — never that something is unwatched that should not
    have been.
    """
    if not enabled:
        return {}
    return {
        "viewCount>=": max(1, rule.min_view_count),
        "lastViewedAt<=": cutoff_timestamp(ctx.now, rule.age_value, rule.age_unit),
    }


async def collect(
    *,
    server: PlexServer,
    token: str,
    rule: Rule,
    ctx: EvalContext,
    libraries: Sequence[dict[str, Any]],
    server_side_filters: bool = True,
) -> Collected:
    """Gather everything `rule` should be evaluated against for this token."""
    out = Collected()
    for library in libraries:
        section_key = str(library["section_key"])
        if rule.media_type == "show":
            await _collect_episodes(
                server, section_key, token, rule, ctx, server_side_filters, out
            )
        else:
            await _collect_movies(
                server, section_key, token, rule, ctx, server_side_filters, out
            )
        out.libraries_scanned += 1
    return out


async def _collect_movies(
    server: PlexServer,
    section_key: str,
    token: str,
    rule: Rule,
    ctx: EvalContext,
    server_side_filters: bool,
    out: Collected,
) -> None:
    async for item in server.iter_section_items(
        section_key,
        token,
        item_type=TYPE_MOVIE,
        extra_params=prefilter(rule, ctx, server_side_filters),
    ):
        out.items.append(item)


async def _collect_episodes(
    server: PlexServer,
    section_key: str,
    token: str,
    rule: Rule,
    ctx: EvalContext,
    server_side_filters: bool,
    out: Collected,
) -> None:
    """Shows first, then episodes only for shows that could yield a match.

    Fetching every episode of every show would be the obvious approach and also
    the slowest: the show listing carries viewedLeafCount, so shows with nothing
    watched — and, when the gate is on, shows that are not finished — can be
    skipped without ever asking for their episodes.

    Note the show listing is deliberately NOT server-side filtered: those
    filters describe episode-level fields, and applying them to the show query
    would hide shows whose episodes still qualify.
    """
    shows: list[MediaItem] = []
    async for show in server.iter_section_items(section_key, token, item_type=TYPE_SHOW):
        shows.append(show)

    out.shows.extend(shows)
    ctx.series_complete.update(build_series_index(shows))
    show_index = {show.rating_key: show for show in shows}

    interesting = []
    for show in shows:
        if not show.viewed_leaf_count:
            continue  # nothing watched in this series at all
        if rule.require_series_complete and not show.fully_watched_series:
            continue  # protected: partly-watched series stay untouched
        interesting.append(show)

    log.info(
        "[%s]   %d show(s) in this library; %d worth opening "
        "(the rest have nothing watched%s)",
        rule.name,
        len(shows),
        len(interesting),
        " or are not finished" if rule.require_series_complete else "",
    )

    for show in interesting:
        for episode in await server.all_leaves(show.rating_key, token):
            # Collections/labels/genres live on the show, so push them down
            # before filtering, or "exclude this collection" would never match
            # an episode.
            out.items.append(inherit_show_tags(episode, show_index.get(show.rating_key)))
