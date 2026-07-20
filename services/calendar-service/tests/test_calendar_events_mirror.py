"""Dirty-check behaviour of the D1 calendar-events mirror.

The sync loop re-sends every calendar event on every tick, so the mirror must
collapse repeats into a single D1 write — see _MIRROR_CACHE in the forwarder.
"""
from datetime import datetime, timezone

import pytest

from meeting_api.collector import d1_calendar_events_forwarder as fwd


ROW = dict(
    event_id=1,
    owner_email="Research@Muns.io",
    title="Standup",
    start_time=datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc),
    end_time=datetime(2026, 7, 20, 11, 30, tzinfo=timezone.utc),
    meeting_url="https://meet.google.com/abc-defg-hij",
    platform="google_meet",
    status="pending",
    meeting_id=None,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    fwd._MIRROR_CACHE.clear()
    yield
    fwd._MIRROR_CACHE.clear()


@pytest.fixture
def calls(monkeypatch):
    """Record every d1_query call; pretend each one succeeds."""
    recorded = []

    async def fake_d1_query(sql, params):
        recorded.append((sql, params))
        return []

    monkeypatch.setattr(fwd, "d1_query", fake_d1_query)
    return recorded


@pytest.mark.asyncio
async def test_repeat_mirror_writes_once(calls):
    for _ in range(5):
        await fwd.mirror_calendar_event_to_d1(**ROW)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_changed_field_writes_again(calls):
    await fwd.mirror_calendar_event_to_d1(**ROW)
    await fwd.mirror_calendar_event_to_d1(**{**ROW, "status": "scheduled"})
    await fwd.mirror_calendar_event_to_d1(**{**ROW, "status": "scheduled"})
    assert len(calls) == 2
    assert calls[-1][1][-2] == "scheduled"


@pytest.mark.asyncio
async def test_failed_write_is_retried(monkeypatch):
    """A failed D1 write must not poison the cache into skipping the retry."""
    recorded = []
    outcomes = [None, []]  # first call fails, second succeeds

    async def flaky_d1_query(sql, params):
        recorded.append(params)
        return outcomes[len(recorded) - 1]

    monkeypatch.setattr(fwd, "d1_query", flaky_d1_query)

    await fwd.mirror_calendar_event_to_d1(**ROW)
    assert fwd._MIRROR_CACHE == {}
    await fwd.mirror_calendar_event_to_d1(**ROW)
    assert len(recorded) == 2
    # Now cached, so a third identical call is a no-op.
    await fwd.mirror_calendar_event_to_d1(**ROW)
    assert len(recorded) == 2


@pytest.mark.asyncio
async def test_params_align_with_insert_columns(calls):
    await fwd.mirror_calendar_event_to_d1(**ROW)
    sql, params = calls[0]
    assert "(id, owner_email, title, start_time_ms, end_time_ms, meeting_url, platform, status, meeting_id)" in sql
    assert params[0] == "1"
    assert params[1] == "research@muns.io"  # normalized before both write and fingerprint
    assert params[2] == "Standup"
    assert params[3] == str(int(ROW["start_time"].timestamp() * 1000))
    assert params[6] == "google_meet"
    assert params[8] is None  # meeting_id stays JSON null, not "None"


@pytest.mark.asyncio
async def test_cache_is_bounded(calls, monkeypatch):
    monkeypatch.setattr(fwd, "_MIRROR_CACHE_MAX", 3)
    for i in range(1, 5):
        await fwd.mirror_calendar_event_to_d1(**{**ROW, "event_id": i})
    assert len(fwd._MIRROR_CACHE) <= 3
