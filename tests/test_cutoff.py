"""Timer arithmetic.

Two different rules apply, and mixing them up is a real bug:

  hours/days/weeks  exact epoch arithmetic. Going through a naive local
                    datetime shifts the answer by an hour whenever the window
                    spans a DST change — drift for no benefit, since "90 days"
                    means 90 x 86400.
  months/years      real calendar arithmetic, because "3 months" should mean
                    what a person means by it. Day-of-month is clamped.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.engine.rules import cutoff_timestamp, describe_threshold
from tests.support import DAY, NOW


@pytest.mark.parametrize(
    "value,unit,seconds",
    [
        (1, "hours", 3600),
        (36, "hours", 36 * 3600),
        (1, "days", DAY),
        (90, "days", 90 * DAY),
        (2, "weeks", 2 * 604800),
    ],
)
def test_fixed_units_are_exact_epoch_arithmetic(value, unit, seconds):
    assert cutoff_timestamp(NOW, value, unit) == NOW - seconds


def test_zero_means_everything_watched_qualifies():
    assert cutoff_timestamp(NOW, 0, "days") == NOW
    assert cutoff_timestamp(NOW, -5, "days") == NOW


@pytest.mark.parametrize(
    "when",
    [
        datetime(2025, 3, 15, 12, 0),   # spans the spring-forward change
        datetime(2025, 11, 15, 12, 0),  # spans the fall-back change
    ],
)
def test_fixed_units_do_not_drift_across_a_dst_change(when):
    moment = int(when.timestamp())
    assert cutoff_timestamp(moment, 30, "days") == moment - 30 * DAY


def test_months_use_calendar_arithmetic():
    moment = int(datetime(2025, 6, 15, 12, 0).timestamp())
    result = datetime.fromtimestamp(cutoff_timestamp(moment, 3, "months"))
    assert (result.year, result.month, result.day) == (2025, 3, 15)


def test_month_arithmetic_clamps_the_day():
    """31 March minus one month is 28 February, not 3 March."""
    moment = int(datetime(2025, 3, 31, 12, 0).timestamp())
    result = datetime.fromtimestamp(cutoff_timestamp(moment, 1, "months"))
    assert (result.year, result.month, result.day) == (2025, 2, 28)


def test_month_arithmetic_handles_a_leap_year():
    moment = int(datetime(2024, 3, 31, 12, 0).timestamp())
    result = datetime.fromtimestamp(cutoff_timestamp(moment, 1, "months"))
    assert (result.year, result.month, result.day) == (2024, 2, 29)


def test_months_roll_back_across_a_year_boundary():
    moment = int(datetime(2025, 2, 10, 12, 0).timestamp())
    result = datetime.fromtimestamp(cutoff_timestamp(moment, 4, "months"))
    assert (result.year, result.month) == (2024, 10)


def test_years_are_calendar_years():
    moment = int(datetime(2025, 3, 31, 12, 0).timestamp())
    result = datetime.fromtimestamp(cutoff_timestamp(moment, 2, "years"))
    assert (result.year, result.month, result.day) == (2023, 3, 31)


def test_a_month_is_not_thirty_days():
    """A 30-day approximation would make February and July behave differently
    for the same rule, which is exactly what people complain about."""
    moment = int(datetime(2025, 3, 31, 12, 0).timestamp())
    assert cutoff_timestamp(moment, 1, "months") != moment - 30 * DAY


def test_unknown_unit_raises():
    with pytest.raises(ValueError, match="fortnights"):
        cutoff_timestamp(NOW, 1, "fortnights")


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (1, "days", "1 day"),
        (2, "days", "2 days"),
        (1, "weeks", "1 week"),
        (3, "months", "3 months"),
        (0, "days", "any watched item"),
    ],
)
def test_describe_threshold(value, unit, expected):
    assert describe_threshold(value, unit) == expected
