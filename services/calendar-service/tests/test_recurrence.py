"""Tests for app.recurrence.next_recurring_run — DST-aware recurring
schedule computation for the D1 `schedules` dispatch loop.

Expected values below were cross-checked by running next_recurring_run
directly (not hand-derived from a calendar), particularly the DST cases —
DST offset/weekday arithmetic is exactly the kind of thing worth pinning to
verified output rather than trusting by inspection.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app.recurrence import next_recurring_run


def _ms(y, mo, d, h, mi, tz="UTC"):
    return int(datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).timestamp() * 1000)


def test_daily_same_day_when_run_time_still_ahead():
    now_ms = _ms(2026, 6, 1, 8, 0)
    next_ms = next_recurring_run(now_ms, "daily", "UTC", 9, 0)
    assert next_ms == _ms(2026, 6, 1, 9, 0)


def test_daily_rolls_to_tomorrow_once_run_time_has_passed():
    now_ms = _ms(2026, 6, 1, 10, 0)
    next_ms = next_recurring_run(now_ms, "daily", "UTC", 9, 0)
    assert next_ms == _ms(2026, 6, 2, 9, 0)


def test_weekdays_skips_the_weekend():
    # 2026-06-05 is a Friday; the next weekday occurrence is Monday 2026-06-08.
    now_ms = _ms(2026, 6, 5, 10, 0)
    next_ms = next_recurring_run(now_ms, "weekdays", "UTC", 9, 0)
    assert next_ms == _ms(2026, 6, 8, 9, 0)


def test_weekly_matches_configured_weekday():
    # weekday=3 (Wed, column convention 0=Sun..6=Sat). "now" is Monday.
    now_ms = _ms(2026, 6, 1, 8, 0)
    next_ms = next_recurring_run(now_ms, "weekly", "UTC", 9, 0, weekday=3)
    assert next_ms == _ms(2026, 6, 3, 9, 0)  # Wednesday


def test_once_must_not_be_scheduled_here():
    with pytest.raises(ValueError):
        next_recurring_run(0, "once", "UTC", 9, 0)


def test_weekly_requires_a_weekday():
    with pytest.raises(ValueError):
        next_recurring_run(0, "weekly", "UTC", 9, 0)


def test_spring_forward_gap_resolves_to_shifted_wall_time():
    # 2027-03-14: America/New_York jumps 2:00 AM -> 3:00 AM. A 2:30 AM daily
    # schedule doesn't exist that day; it should resolve to the wall-clock
    # equivalent after the jump (3:30 AM EDT), not silently land elsewhere.
    now_ms = _ms(2027, 3, 13, 12, 0, tz="America/New_York")
    next_ms = next_recurring_run(now_ms, "daily", "America/New_York", 2, 30)
    local = datetime.fromtimestamp(next_ms / 1000, tz=timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    )
    assert (local.year, local.month, local.day) == (2027, 3, 14)
    assert (local.hour, local.minute) == (3, 30)


def test_fall_back_ambiguous_time_picks_first_occurrence():
    # 2026-11-01: America/New_York falls back 2:00 AM -> 1:00 AM, so 1:30 AM
    # occurs twice. fold=0 must pick the FIRST (pre-transition, EDT) instant.
    now_ms = _ms(2026, 10, 31, 12, 0, tz="America/New_York")
    next_ms = next_recurring_run(now_ms, "daily", "America/New_York", 1, 30)
    first_occurrence_utc = (
        datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        .replace(fold=0)
        .astimezone(timezone.utc)
    )
    second_occurrence_utc = (
        datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"))
        .replace(fold=1)
        .astimezone(timezone.utc)
    )
    result = datetime.fromtimestamp(next_ms / 1000, tz=timezone.utc)
    assert result == first_occurrence_utc
    assert result != second_occurrence_utc


def test_weekly_continues_correctly_across_a_dst_boundary():
    # weekday=6 (Sat). 2027-03-13 (Sat) is just before the 2027-03-14
    # spring-forward Sunday; the next Saturday occurrence must still land on
    # 9:00 AM local time on 2027-03-20, on the far side of the transition.
    now_ms = _ms(2027, 3, 13, 10, 0, tz="America/New_York")
    next_ms = next_recurring_run(
        now_ms, "weekly", "America/New_York", 9, 0, weekday=6
    )
    local = datetime.fromtimestamp(next_ms / 1000, tz=timezone.utc).astimezone(
        ZoneInfo("America/New_York")
    )
    assert (local.year, local.month, local.day) == (2027, 3, 20)
    assert (local.hour, local.minute) == (9, 0)
