"""The rule engine's gate matrix, against recorded Plex payloads.

`engine/rules.py` is pure, so every one of these runs without a database, a
network, or a clock. This is where the app's correctness is actually pinned
down.
"""

from __future__ import annotations

import pytest

from app.engine.rules import (
    Decision,
    Filter,
    Reason,
    build_series_index,
    collapse_to_series,
    evaluate,
    evaluate_item,
    inherit_show_tags,
    summarise_skips,
)
from app.plex.types import MediaItem
from tests.support import DAY, NOW, by_title, ctx, episode, episodes, movie, movies, rule, shows


# ---------------------------------------------------------------------------
# Parsing the recorded payloads
# ---------------------------------------------------------------------------

def test_fixtures_cover_the_interesting_states():
    parsed = movies()
    assert len(parsed) == 6
    assert any(m.view_count == 0 for m in parsed), "need a never-watched item"
    assert any(
        m.view_count > 0 and m.last_viewed_at is None for m in parsed
    ), "need a watched item with no lastViewedAt"
    assert any(m.in_progress for m in parsed), "need an item with a resume point"


def test_episode_parsing():
    first = episodes()[0]
    assert first.type == "episode"
    assert first.grandparent_rating_key
    assert first.display_title.startswith(first.grandparent_title)
    assert "S01E01" in first.display_title


def test_series_completion_comes_from_leaf_counts():
    index = build_series_index(shows())
    assert True in index.values() and False in index.values(), index


# ---------------------------------------------------------------------------
# Core gates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "item,applied_rule,matched,reason",
    [
        (movie(), rule(), True, Reason.MATCHED),
        (movie(view_count=0), rule(), False, Reason.NOT_WATCHED),
        (movie(view_count=1), rule(min_view_count=3), False, Reason.BELOW_MIN_VIEWS),
        (movie(last_viewed_at=None), rule(), False, Reason.NO_WATCH_DATE),
        (movie(last_viewed_at=NOW - 5 * DAY), rule(), False, Reason.TOO_RECENT),
        (movie(view_offset=500), rule(), False, Reason.IN_PROGRESS),
        (movie(view_offset=500), rule(skip_in_progress=False), True, Reason.MATCHED),
        (movie(view_count=3), rule(min_view_count=3), True, Reason.MATCHED),
    ],
)
def test_gate_matrix(item, applied_rule, matched, reason):
    decision = evaluate_item(item, applied_rule, ctx())
    assert decision.matched is matched
    assert decision.reason == reason


def test_missing_watch_date_is_skipped_not_guessed():
    """The safe call: the alternative is silently wiping history we cannot date."""
    decision = evaluate_item(movie(last_viewed_at=None), rule(age_value=0), ctx())
    assert decision.reason == Reason.NO_WATCH_DATE


def test_never_watched_is_never_touched_even_at_zero_threshold():
    decision = evaluate_item(movie(view_count=0), rule(age_value=0), ctx())
    assert not decision.matched


@pytest.mark.parametrize("offset,expected", [(-1, True), (0, True), (1, False)])
def test_age_boundary_is_inclusive(offset, expected):
    from app.engine.rules import cutoff_timestamp

    cutoff = cutoff_timestamp(NOW, 90, "days")
    decision = evaluate_item(movie(last_viewed_at=cutoff + offset), rule(), ctx())
    assert decision.matched is expected


def test_now_playing_is_protected():
    assert (
        evaluate_item(movie(), rule(), ctx(now_playing={"m1"})).reason
        == Reason.NOW_PLAYING
    )
    assert evaluate_item(
        movie(), rule(skip_now_playing=False), ctx(now_playing={"m1"})
    ).matched


# ---------------------------------------------------------------------------
# TV
# ---------------------------------------------------------------------------

def test_series_gate_protects_a_partly_watched_show():
    decision = evaluate_item(
        episode(), rule(media_type="show"), ctx(series_complete={"s1": False})
    )
    assert decision.reason == Reason.SERIES_INCOMPLETE


def test_unknown_series_is_treated_as_incomplete():
    """Absence of information must not read as permission."""
    decision = evaluate_item(episode(), rule(media_type="show"), ctx(series_complete={}))
    assert decision.reason == Reason.SERIES_INCOMPLETE


def test_series_gate_off_reaches_a_partial_series():
    decision = evaluate_item(
        episode(),
        rule(media_type="show", require_series_complete=False),
        ctx(series_complete={"s1": False}),
    )
    assert decision.matched


def test_episode_with_no_show_key_is_incomplete():
    decision = evaluate_item(
        episode(grandparent_rating_key=None),
        rule(media_type="show"),
        ctx(series_complete={"s1": True}),
    )
    assert decision.reason == Reason.SERIES_INCOMPLETE


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

def test_include_filter_by_genre():
    tagged = movie(genres={"sci-fi"})
    assert evaluate_item(tagged, rule(include_filters=[Filter("genre", "Sci-Fi")]), ctx()).matched
    assert (
        evaluate_item(tagged, rule(include_filters=[Filter("genre", "horror")]), ctx()).reason
        == Reason.NOT_INCLUDED
    )


def test_exclude_filter_by_collection():
    tagged = movie(collections={"villeneuve"})
    decision = evaluate_item(
        tagged, rule(exclude_filters=[Filter("collection", "Villeneuve")]), ctx()
    )
    assert decision.reason == Reason.EXCLUDED
    assert "villeneuve" in decision.detail.lower()


def test_exclude_beats_include():
    """If something matches both, it stays watched. Destructive-by-accident is
    the failure mode worth designing against."""
    tagged = movie(genres={"sci-fi"}, labels={"keep"})
    decision = evaluate_item(
        tagged,
        rule(
            include_filters=[Filter("genre", "sci-fi")],
            exclude_filters=[Filter("label", "keep")],
        ),
        ctx(),
    )
    assert decision.reason == Reason.EXCLUDED


def test_filters_are_case_insensitive():
    assert evaluate_item(
        movie(labels={"keepme"}), rule(exclude_filters=[Filter("label", "KEEPME")]), ctx()
    ).reason == Reason.EXCLUDED


def test_empty_filter_value_never_matches():
    decision = evaluate_item(
        movie(genres={"sci-fi"}), rule(include_filters=[Filter("genre", "   ")]), ctx()
    )
    assert decision.reason == Reason.NOT_INCLUDED


def test_unknown_filter_field_never_matches():
    assert not Filter("director", "Villeneuve").matches(movie())


def test_title_filter_matches_the_series_name():
    decision = evaluate_item(
        episode(),
        rule(media_type="show", require_series_complete=False,
             include_filters=[Filter("title", "a show")]),
        ctx(),
    )
    assert decision.matched


def test_show_tags_are_inherited_by_episodes():
    """'Exclude the Marvel collection' has to keep that show's episodes."""
    show = MediaItem(
        rating_key="s1", type="show", title="A Show",
        collections={"marvel"}, genres={"action"}, labels={"keep"},
    )
    merged = inherit_show_tags(episode(), show)
    assert merged.collections == {"marvel"}
    assert merged.genres == {"action"}
    assert merged.labels == {"keep"}

    decision = evaluate_item(
        merged,
        rule(media_type="show", require_series_complete=False,
             exclude_filters=[Filter("collection", "marvel")]),
        ctx(),
    )
    assert decision.reason == Reason.EXCLUDED


def test_inherit_show_tags_tolerates_a_missing_show():
    original = episode()
    assert inherit_show_tags(original, None) is original


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def test_evaluate_splits_matched_and_skipped():
    matched, skipped = evaluate(movies(), rule(), ctx())
    assert matched and skipped
    assert len(matched) + len(skipped) == len(movies())


def test_summarise_skips_orders_by_frequency():
    skipped = [
        Decision(movie(), False, Reason.TOO_RECENT),
        Decision(movie(), False, Reason.TOO_RECENT),
        Decision(movie(), False, Reason.NOT_WATCHED),
    ]
    summary = summarise_skips(skipped)
    assert summary[0] == ("Watched too recently", 2)
    assert summary[1] == ("Never watched", 1)


def test_decision_text_includes_detail():
    decision = evaluate_item(movie(view_count=1), rule(min_view_count=5), ctx())
    assert "needs 5x" in decision.text


# ---------------------------------------------------------------------------
# tv_scope = series
# ---------------------------------------------------------------------------

def _matched_episodes(count: int, show_key: str = "s1") -> list[Decision]:
    return [
        Decision(
            episode(rating_key=f"e{i}", grandparent_rating_key=show_key),
            True,
            Reason.MATCHED,
        )
        for i in range(count)
    ]


def test_series_scope_collapses_a_fully_matched_show():
    show = MediaItem(rating_key="s1", type="show", title="A Show",
                     leaf_count=3, viewed_leaf_count=3)
    collapsed = collapse_to_series(_matched_episodes(3), [show])
    assert len(collapsed) == 1
    assert collapsed[0].item.rating_key == "s1"
    assert "3 episodes" in collapsed[0].detail


def test_series_scope_does_not_collapse_a_partial_match():
    """Collapsing would unwatch episodes the rule deliberately protected."""
    show = MediaItem(rating_key="s1", type="show", title="A Show",
                     leaf_count=5, viewed_leaf_count=5)
    collapsed = collapse_to_series(_matched_episodes(2), [show])
    assert len(collapsed) == 2
    assert all(d.item.type == "episode" for d in collapsed)


def test_series_scope_leaves_unknown_shows_alone():
    collapsed = collapse_to_series(_matched_episodes(2, show_key="missing"), [])
    assert len(collapsed) == 2
