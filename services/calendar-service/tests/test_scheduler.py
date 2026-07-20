"""Tests for app.scheduler — the D1 `schedules` dispatch loop.

Mocks the D1 schedule client functions and the /public/join HTTP call to
verify the per-row state machine: recurring rows advance next_run and reset
attempts; one-time rows delete on success (200/201, and 409 — meeting-api
already dedups by native_meeting_id, so a duplicate join is a no-op); one-time
rows retry (unchanged next_run, attempts+1) until SCHEDULER_MAX_ATTEMPTS, then
delete.
"""
import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app import scheduler


def _resp(status_code):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.text = ""
    return r


def _mock_httpx_client(mock_client_cls, response):
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=response)
    mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_client


@pytest.fixture(autouse=True)
def _join_api_key():
    with patch.object(scheduler, "JOIN_API_KEY", "test-key"):
        yield


@pytest.mark.asyncio
async def test_once_row_deleted_on_success():
    row = {
        "id": "sched-1",
        "owner": "a@b.com",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "recurrence": "once",
        "attempts": 0,
    }
    with patch.object(scheduler, "select_due_schedules", AsyncMock(return_value=[row])), \
         patch.object(scheduler, "delete_schedule_row", AsyncMock(return_value=True)) as mock_delete, \
         patch.object(scheduler, "update_failed_attempt", AsyncMock()) as mock_fail, \
         patch("httpx.AsyncClient") as mock_client_cls:
        _mock_httpx_client(mock_client_cls, _resp(200))
        count = await scheduler.dispatch_due_schedules()

    assert count == 1
    mock_delete.assert_awaited_once_with("sched-1")
    mock_fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_409_is_treated_as_success_not_a_retry():
    # meeting-api already dedups bot joins by native_meeting_id, so a
    # duplicate /public/join for an already-active meeting is a no-op.
    row = {
        "id": "sched-2",
        "owner": "a@b.com",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "recurrence": "once",
        "attempts": 0,
    }
    with patch.object(scheduler, "select_due_schedules", AsyncMock(return_value=[row])), \
         patch.object(scheduler, "delete_schedule_row", AsyncMock()) as mock_delete, \
         patch.object(scheduler, "update_failed_attempt", AsyncMock()) as mock_fail, \
         patch("httpx.AsyncClient") as mock_client_cls:
        _mock_httpx_client(mock_client_cls, _resp(409))
        await scheduler.dispatch_due_schedules()

    mock_delete.assert_awaited_once_with("sched-2")
    mock_fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_once_row_survives_failure_below_max_attempts():
    row = {
        "id": "sched-3",
        "owner": "a@b.com",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "recurrence": "once",
        "attempts": 0,
    }
    with patch.object(scheduler, "select_due_schedules", AsyncMock(return_value=[row])), \
         patch.object(scheduler, "delete_schedule_row", AsyncMock()) as mock_delete, \
         patch.object(scheduler, "update_failed_attempt", AsyncMock(return_value=True)) as mock_fail, \
         patch("httpx.AsyncClient") as mock_client_cls:
        _mock_httpx_client(mock_client_cls, _resp(500))
        await scheduler.dispatch_due_schedules()

    mock_delete.assert_not_awaited()
    mock_fail.assert_awaited_once()
    assert mock_fail.call_args.kwargs["attempts"] == 1


@pytest.mark.asyncio
async def test_once_row_deleted_after_max_attempts_exhausted():
    row = {
        "id": "sched-4",
        "owner": "a@b.com",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "recurrence": "once",
        "attempts": scheduler.SCHEDULER_MAX_ATTEMPTS - 1,
    }
    with patch.object(scheduler, "select_due_schedules", AsyncMock(return_value=[row])), \
         patch.object(scheduler, "delete_schedule_row", AsyncMock(return_value=True)) as mock_delete, \
         patch.object(scheduler, "update_failed_attempt", AsyncMock()) as mock_fail, \
         patch("httpx.AsyncClient") as mock_client_cls:
        _mock_httpx_client(mock_client_cls, _resp(500))
        await scheduler.dispatch_due_schedules()

    mock_delete.assert_awaited_once_with("sched-4")
    mock_fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_recurring_row_advances_next_run_and_resets_attempts():
    row = {
        "id": "sched-5",
        "owner": "a@b.com",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "recurrence": "daily",
        "time_zone": "UTC",
        "hour": 9,
        "minute": 0,
        "weekday": None,
        "attempts": 2,
    }
    with patch.object(scheduler, "select_due_schedules", AsyncMock(return_value=[row])), \
         patch.object(scheduler, "update_recurring_row", AsyncMock(return_value=True)) as mock_update, \
         patch.object(scheduler, "delete_schedule_row", AsyncMock()) as mock_delete, \
         patch("httpx.AsyncClient") as mock_client_cls:
        _mock_httpx_client(mock_client_cls, _resp(200))
        await scheduler.dispatch_due_schedules()

    mock_delete.assert_not_awaited()
    mock_update.assert_awaited_once()
    assert mock_update.call_args.kwargs["last_status"] == "sent"
    assert mock_update.call_args.kwargs["next_run"] > 0


@pytest.mark.asyncio
async def test_missing_api_key_fails_without_raising():
    row = {
        "id": "sched-6",
        "owner": "a@b.com",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "recurrence": "once",
        "attempts": 0,
    }
    with patch.object(scheduler, "JOIN_API_KEY", ""), \
         patch.object(scheduler, "select_due_schedules", AsyncMock(return_value=[row])), \
         patch.object(scheduler, "delete_schedule_row", AsyncMock()) as mock_delete, \
         patch.object(scheduler, "update_failed_attempt", AsyncMock(return_value=True)) as mock_fail:
        await scheduler.dispatch_due_schedules()

    mock_delete.assert_not_awaited()
    mock_fail.assert_awaited_once()
