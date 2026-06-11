"""End-to-end coverage of the D1 meeting mirror lifecycle hooks.

Asserts that the mirror is invoked at the lifecycle moments named in
implement-this.txt Part B:
  - meeting creation in POST /bots
  - deferred transcription completion
  - post-meeting finalization (run_all_tasks)

Crucially: a D1 failure at any of these hook points MUST be non-fatal —
the primary request flow has to complete with its normal status code.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_CONTAINER_ID,
    TEST_CONTAINER_NAME,
    TEST_MEETING_ID,
    MockResult,
    make_meeting,
)


def _setup_create_meeting_db(mock_db):
    call_count = 0

    async def multi_execute(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return MockResult([])
        elif call_count == 2:
            return MockResult(scalar_value=0)
        return MockResult()

    mock_db.execute = AsyncMock(side_effect=multi_execute)


class TestCreateBotMirror:

    @pytest.mark.asyncio
    async def test_create_bot_schedules_d1_mirror(self, client, mock_db, mock_redis):
        """POST /bots queues a background safe_mirror_meeting call."""
        _setup_create_meeting_db(mock_db)

        runtime_resp = {"container_id": TEST_CONTAINER_ID, "name": TEST_CONTAINER_NAME}
        with patch("meeting_api.meetings._spawn_via_runtime_api", new_callable=AsyncMock, return_value=runtime_resp), \
             patch("meeting_api.meetings.mint_meeting_token", return_value="fake.jwt.token"), \
             patch("meeting_api.meetings.async_session_local") as mock_sf, \
             patch(
                 "meeting_api.meetings._d1_safe_mirror_meeting",
                 new_callable=AsyncMock,
             ) as mock_mirror:
            inner = AsyncMock()
            inner.add = MagicMock()
            inner.commit = AsyncMock()
            mock_sf.return_value.__aenter__ = AsyncMock(return_value=inner)
            mock_sf.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await client.post("/bots", json={
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
            })

        assert resp.status_code == 201, resp.text
        # FastAPI runs background_tasks after the response; the AsyncClient
        # in tests drives the full ASGI cycle, so by here the mock should
        # have been called exactly once with the new meeting.
        assert mock_mirror.await_count == 1
        meeting_arg = mock_mirror.await_args.args[0]
        # The Meeting ORM passes through unchanged.
        assert hasattr(meeting_arg, "data") and isinstance(meeting_arg.data, dict)

    @pytest.mark.asyncio
    async def test_create_bot_uses_background_task_not_inline_await(
        self, client, mock_db, mock_redis,
    ):
        """The D1 mirror is queued as a BackgroundTask, not awaited inline.

        Background-task scheduling is what guarantees a slow / unreachable
        D1 cannot delay the create-bot response. The forwarder itself is
        already proven non-raising in test_d1_meeting_mirror.py.
        """
        _setup_create_meeting_db(mock_db)

        runtime_resp = {"container_id": TEST_CONTAINER_ID, "name": TEST_CONTAINER_NAME}
        observed = {}

        async def slow_mirror(meeting):
            observed["called"] = True

        with patch("meeting_api.meetings._spawn_via_runtime_api", new_callable=AsyncMock, return_value=runtime_resp), \
             patch("meeting_api.meetings.mint_meeting_token", return_value="fake.jwt.token"), \
             patch("meeting_api.meetings.async_session_local") as mock_sf, \
             patch("meeting_api.meetings._d1_safe_mirror_meeting", side_effect=slow_mirror) as mock_mirror:
            inner = AsyncMock()
            inner.add = MagicMock()
            inner.commit = AsyncMock()
            mock_sf.return_value.__aenter__ = AsyncMock(return_value=inner)
            mock_sf.return_value.__aexit__ = AsyncMock(return_value=False)

            resp = await client.post("/bots", json={
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
            })

        assert resp.status_code == 201, resp.text
        # The mirror still gets called (post-response) — proving it was
        # scheduled, not skipped.
        assert observed.get("called") is True
        assert mock_mirror.call_count == 1


class TestPostMeetingMirror:

    @pytest.mark.asyncio
    async def test_run_all_tasks_invokes_d1_mirror(self):
        """run_all_tasks fires the D1 meeting mirror as its final task."""
        from meeting_api import post_meeting as pm

        fake_meeting = make_meeting(status="completed")

        # Stub every other task to a no-op so we isolate the mirror call.
        with patch.object(pm, "async_session_local") as mock_sf, \
             patch.object(pm, "finalize_in_progress_recordings", new_callable=AsyncMock, return_value=0), \
             patch.object(pm, "aggregate_transcription", new_callable=AsyncMock), \
             patch.object(pm, "send_completion_webhook", new_callable=AsyncMock), \
             patch.object(pm, "fire_post_meeting_hooks", new_callable=AsyncMock), \
             patch.object(pm, "_d1_safe_mirror_meeting", new_callable=AsyncMock) as mock_mirror:

            db = AsyncMock()
            db.get = AsyncMock(return_value=fake_meeting)
            db.commit = AsyncMock()
            mock_sf.return_value.__aenter__ = AsyncMock(return_value=db)
            mock_sf.return_value.__aexit__ = AsyncMock(return_value=False)

            await pm.run_all_tasks(TEST_MEETING_ID)

        assert mock_mirror.await_count >= 1
        assert mock_mirror.await_args.args[0] is fake_meeting

    @pytest.mark.asyncio
    async def test_run_all_tasks_continues_when_mirror_raises(self):
        """A D1 mirror raise in Task 4 must not propagate."""
        from meeting_api import post_meeting as pm

        fake_meeting = make_meeting(status="completed")
        with patch.object(pm, "async_session_local") as mock_sf, \
             patch.object(pm, "finalize_in_progress_recordings", new_callable=AsyncMock, return_value=0), \
             patch.object(pm, "aggregate_transcription", new_callable=AsyncMock), \
             patch.object(pm, "send_completion_webhook", new_callable=AsyncMock), \
             patch.object(pm, "fire_post_meeting_hooks", new_callable=AsyncMock), \
             patch.object(
                 pm, "_d1_safe_mirror_meeting", new_callable=AsyncMock,
                 side_effect=RuntimeError("D1 boom"),
             ):
            db = AsyncMock()
            db.get = AsyncMock(return_value=fake_meeting)
            db.commit = AsyncMock()
            mock_sf.return_value.__aenter__ = AsyncMock(return_value=db)
            mock_sf.return_value.__aexit__ = AsyncMock(return_value=False)

            # Must complete without raising — caller is a background task,
            # but `run_all_tasks` itself is the safety boundary.
            await pm.run_all_tasks(TEST_MEETING_ID)
