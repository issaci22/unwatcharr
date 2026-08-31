"""Rule evaluation.

This module is PURE: no HTTP, no database, no clock of its own. It takes already
fetched items plus an explicit `now`, and returns a decision for every one. That
is what makes the whole gate matrix testable from fixtures without a live Plex,
and it is where correctness actually lives — the server-side filters sent in
plex/client.py are only a payload optimisation, since Plex silently ignores
filters it does not recognise and hands back the entire library.

The engine knows nothing about per-user overrides. `store.build_rule()` resolves
an override into an effective threshold before constructing a `Rule`.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Sequence

from ..plex.types import MediaItem

AGE_UNITS = ("hours", "days", "weeks", "months", "years")
FILTER_FIELDS = ("collection", "label", "genre", "title")
MEDIA_TYPES = ("movie", "show")
TV_SCOPES = ("episodes", "series")


class Reason:
    MATCHED = "matched"
    NOT_WATCHED = "not_watched"
    BELOW_MIN_VIEWS = "below_min_views"
    TOO_RECENT = "too_recent"
    NO_WATCH_DATE = "no_watch_date"
    IN_PROGRESS = "in_progress"
    NOW_PLAYING = "now_playing"
    SERIES_INCOMPLETE = "series_incomplete"
    EXCLUDED = "excluded"
    NOT_INCLUDED = "not_included"
    ALREADY_UNWATCHED = "already_unwatched"


REASON_TEXT = {
    Reason.MATCHED: "Will be marked unwatched",
    Reason.NOT_WATCHED: "Never watched",
    Reason.BELOW_MIN_VIEWS: "Watched fewer times than the minimum",
    Reason.TOO_RECENT: "Watched too recently",
    Reason.NO_WATCH_DATE: "Plex has no watch date for this item",
    Reason.IN_PROGRESS: "Partly watched (resume point set)",
    Reason.NOW_PLAYING: "Being played right now",
    Reason.SERIES_INCOMPLETE: "Series is not fully watched",
    Reason.EXCLUDED: "Matched an exclude filter",
    Reason.NOT_INCLUDED: "Did not match any include filter",
    Reason.ALREADY_UNWATCHED: "Already unwatched",
}


@dataclass(frozen=True)
class Filter:
    field: str      # collection | label | genre | title
    value: str

    def matches(self, item: MediaItem) -> bool:
        needle = self.value.strip().lower()
        if not needle:
            return False
        if self.field == "collection":
            return needle in item.collections
        if self.field == "label":
            return needle in item.labels
        if self.field == "genre":
            return needle in item.genres
        if self.field == "title":
            haystacks = [item.title.lower()]
            if item.grandparent_title:
                haystacks.append(item.grandparent_title.lower())
            return any(needle in h for h in haystacks)
        return False

    def describe(self) -> str:
        return f"{self.field}: {self.value}"


@dataclass
class Rule:
    """The evaluable shape of a rule row.

    `age_value`/`age_unit` here are the EFFECTIVE threshold — any per-user
    override has already been applied by store.build_rule().
    """

    id: int = 0
    name: str = "Rule"
    media_type: str = "movie"            # movie | show
    age_value: int = 90
    age_unit: str = "days"
    min_view_count: int = 1
    require_series_complete: bool = True
    skip_in_progress: bool = True
    skip_now_playing: bool = True
    clear_progress: bool = False
    tv_scope: str = "episodes"           # episodes | series
    include_filters: list[Filter] = field(default_factory=list)
    exclude_filters: list[Filter] = field(default_factory=list)


@dataclass
class EvalContext:
    now: int
    now_playing: set[str] = field(default_factory=set)
    # show rating key -> is the whole series watched
    series_complete: dict[str, bool] = field(default_factory=dict)


@dataclass
class Decision:
    item: MediaItem
    matched: bool
    reason: str
    detail: str = ""

    @property
    def text(self) -> str:
        base = REASON_TEXT.get(self.reason, self.reason)
        return f"{base} ({self.detail})" if self.detail else base


def cutoff_timestamp(now: int, value: int, unit: str) -> int:
    """Epoch seconds before which a watch counts as 'old enough'.

    Fixed-length units are exact epoch arithmetic. Going through a naive local
    datetime here would shift the answer by an hour whenever the window spans a
    DST change, which is drift for no benefit: "90 days" means 90 x 86400.

    Months and years use real calendar arithmetic, because "3 months" should
    mean what a person means by it. Day-of-month is clamped, so 31 March minus
    one month is 28/29 February. An hour of DST drift on a window that long is
    irrelevant.
    """
    if value <= 0:
        # "0 days" means everything watched at all qualifies -- the sanity check
        # during setup, and genuinely useful for a one-off purge.
        return now

    if unit == "hours":
        return now - value * 3600
    if unit == "days":
        return now - value * 86400
    if unit == "weeks":
        return now - value * 604800

    moment = datetime.fromtimestamp(now)

    if unit == "months":
        months = value
    elif unit == "years":
        months = value * 12
    else:
        raise ValueError(f"Unknown age unit: {unit!r}")

    total = (moment.year * 12 + (moment.month - 1)) - months
    year, month = divmod(total, 12)
    month += 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return int(moment.replace(year=year, month=month, day=day).timestamp())


def describe_threshold(value: int, unit: str) -> str:
    if value <= 0:
        return "any watched item"
    label = unit if value != 1 else unit.rstrip("s")
    return f"{value} {label}"


def evaluate_item(item: MediaItem, rule: Rule, ctx: EvalContext) -> Decision:
    """Decide a single item.

    Gate order is deliberate: cheap, definitive checks first, so the reason a
    user sees is the most useful one.
    """

    # --- watched at all? ---------------------------------------------------
    if item.view_count <= 0:
        return Decision(item, False, Reason.NOT_WATCHED)

    if item.view_count < max(1, rule.min_view_count):
        return Decision(
            item,
            False,
            Reason.BELOW_MIN_VIEWS,
            f"watched {item.view_count}x, needs {rule.min_view_count}x",
        )

    # --- age ---------------------------------------------------------------
    cutoff = cutoff_timestamp(ctx.now, rule.age_value, rule.age_unit)
    if item.last_viewed_at is None:
        # Watched, but Plex never recorded when. Refusing to guess is the safe
        # call: the alternative is silently wiping history that cannot be dated.
        return Decision(item, False, Reason.NO_WATCH_DATE)
    if item.last_viewed_at > cutoff:
        days = max(0, (ctx.now - item.last_viewed_at) // 86400)
        return Decision(item, False, Reason.TOO_RECENT, f"{days}d ago")

    # --- live state --------------------------------------------------------
    if rule.skip_now_playing and item.rating_key in ctx.now_playing:
        return Decision(item, False, Reason.NOW_PLAYING)

    if rule.skip_in_progress and item.in_progress:
        return Decision(item, False, Reason.IN_PROGRESS)

    # --- TV: only touch a series once it is finished ------------------------
    if rule.require_series_complete and item.type == "episode":
        show_key = item.grandparent_rating_key
        if show_key is None or not ctx.series_complete.get(show_key, False):
            return Decision(item, False, Reason.SERIES_INCOMPLETE)

    # --- filters -----------------------------------------------------------
    # Exclude wins over include: if something is both, it stays watched. That is
    # the less destructive reading, and destructive-by-accident is the failure
    # mode worth designing against here.
    for flt in rule.exclude_filters:
        if flt.matches(item):
            return Decision(item, False, Reason.EXCLUDED, flt.describe())

    if rule.include_filters:
        if not any(f.matches(item) for f in rule.include_filters):
            return Decision(item, False, Reason.NOT_INCLUDED)

    return Decision(item, True, Reason.MATCHED)


def evaluate(
    items: Iterable[MediaItem], rule: Rule, ctx: EvalContext
) -> tuple[list[Decision], list[Decision]]:
    """Evaluate many items. Returns (matched, skipped)."""
    matched: list[Decision] = []
    skipped: list[Decision] = []
    for item in items:
        decision = evaluate_item(item, rule, ctx)
        (matched if decision.matched else skipped).append(decision)
    return matched, skipped


def summarise_skips(skipped: Sequence[Decision]) -> list[tuple[str, int]]:
    """Counts per reason, most common first — drives the run summary.

    Individual skips are never logged, at any level: a library where nothing
    matches would otherwise produce one line per item on every scheduled run.
    The preview is where a per-item answer lives.
    """
    counts: dict[str, int] = {}
    for decision in skipped:
        counts[decision.reason] = counts.get(decision.reason, 0) + 1
    return sorted(
        ((REASON_TEXT.get(r, r), n) for r, n in counts.items()),
        key=lambda pair: (-pair[1], pair[0]),
    )


def build_series_index(shows: Iterable[MediaItem]) -> dict[str, bool]:
    """show rating key -> is every episode watched.

    Uses Plex's own viewedLeafCount/leafCount rather than counting episodes.
    """
    return {show.rating_key: show.fully_watched_series for show in shows}


def inherit_show_tags(episode: MediaItem, show: MediaItem | None) -> MediaItem:
    """Collections, labels and genres live on the show, not the episode.

    'Exclude the Marvel collection' has to keep that show's episodes, so tags
    are pushed down before filtering.
    """
    if show is None:
        return episode
    episode.collections = episode.collections | show.collections
    episode.labels = episode.labels | show.labels
    episode.genres = episode.genres | show.genres
    return episode


def collapse_to_series(
    matched: Sequence[Decision], shows: Sequence[MediaItem]
) -> list[Decision]:
    """Reduce episode decisions to one decision per show.

    Only used when a TV rule is set to `tv_scope = "series"`. Unscrobbling a
    show's rating key clears every episode under it in one call, which is far
    cheaper on a large library — but it is all-or-nothing, so a show is only
    collapsed when EVERY one of its matched episodes is included and nothing
    from that show was skipped for a protective reason. The caller supplies the
    show items; anything that cannot be collapsed stays as episode decisions.
    """
    by_show: dict[str, MediaItem] = {s.rating_key: s for s in shows}
    grouped: dict[str, list[Decision]] = {}
    loose: list[Decision] = []
    for decision in matched:
        key = decision.item.grandparent_rating_key
        if key and key in by_show:
            grouped.setdefault(key, []).append(decision)
        else:
            loose.append(decision)

    out: list[Decision] = []
    for key, decisions in grouped.items():
        show = by_show[key]
        # Every episode Plex knows about must be in the matched set, or
        # collapsing would unwatch episodes the rule deliberately protected.
        if show.leaf_count is not None and len(decisions) >= show.leaf_count:
            out.append(
                Decision(show, True, Reason.MATCHED, f"whole series, {len(decisions)} episodes")
            )
        else:
            out.extend(decisions)
    out.extend(loose)
    return out
