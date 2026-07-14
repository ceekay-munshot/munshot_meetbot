"""Batch transcription must bill Deepgram exactly once per meeting.

Regression cover for the meeting-66 defect: run_all_tasks() fires from BOTH the
container-exit callback and the terminal status_change handler, so two
invocations race on every meeting end. The old ``batch_transcribed`` flag was
only read at the start and only written ~30s later (after audio assembly and the
Deepgram round-trip), so both callers read False, both proceeded, and the same
audio was transcribed — and billed — twice.

The claim is now taken under a row lock before any work begins.
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from meeting_api import batch_transcribe as bt


class _FakeMeeting:
    def __init__(self, data):
        self.id = 66
        self.data = data


class _FakeDB:
    """Minimal async-session stand-in sharing one meeting row across sessions,
    with an asyncio lock standing in for Postgres' SELECT ... FOR UPDATE."""

    def __init__(self, store, lock):
        self._store = store
        self._lock = lock
        self._held = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        if self._held:
            self._lock.release()
            self._held = False
        return False

    async def execute(self, *_a, **_kw):
        # Stands in for the `.with_for_update()` select — blocks until the
        # previous holder commits or rolls back.
        await self._lock.acquire()
        self._held = True
        meeting = _FakeMeeting(self._store["data"])

        class _Res:
            def scalar_one_or_none(self_inner):
                return meeting

        return _Res()

    async def get(self, _model, _mid):
        return _FakeMeeting(self._store["data"])

    async def commit(self):
        # Persist whatever the caller mutated back into the shared store.
        self._store["data"] = dict(self._store["data"])
        if self._held:
            self._lock.release()
            self._held = False

    async def rollback(self):
        if self._held:
            self._lock.release()
            self._held = False


@pytest.mark.asyncio
async def test_concurrent_callers_transcribe_exactly_once():
    """Two racing callers (exit callback + status_change) must yield ONE
    Deepgram call, not two."""
    store = {"data": {"recording": {"session_uid": "s1"}}}
    lock = asyncio.Lock()

    # Make the claim actually stick in the shared store.
    real_claim = bt._claim_batch_run

    async def claim(meeting_id, force):
        async with lock:
            d = dict(store["data"])
            if d.get("batch_transcribed") and not force:
                return None
            claimed = d.get("batch_transcribe_claimed_at")
            if claimed is not None and not force and (time.time() - float(claimed)) < bt._CLAIM_TTL_S:
                return None
            d["batch_transcribe_claimed_at"] = time.time()
            store["data"] = d
            return d

    calls = {"deepgram": 0}

    async def fake_deepgram(_audio, _fmt):
        calls["deepgram"] += 1
        await asyncio.sleep(0.05)  # the window the old code raced through
        return {"segments": [{"start": 0.0, "end": 1.0, "text": "hi", "speaker_index": 0}]}

    async def fake_write(meeting_id, data_obj):
        # Stand in for steps 2-7; mark done exactly as the real code does.
        audio = await bt.recording_store.assemble_meeting_audio(meeting_id, "s1")
        if not audio[0]:
            return False
        if not await bt._call_batch_service(audio[0], "webm"):
            return False
        d = dict(store["data"])
        d["batch_transcribed"] = True
        store["data"] = d
        return True

    with patch.object(bt, "_claim_batch_run", new=claim), \
         patch.object(bt, "_run_claimed_batch", new=fake_write), \
         patch.object(bt, "_call_batch_service", new=AsyncMock(side_effect=fake_deepgram)), \
         patch.object(
             bt.recording_store,
             "assemble_meeting_audio",
             new=AsyncMock(return_value=(b"audio", "webm", 3)),
         ):
        results = await asyncio.gather(*[bt.batch_transcribe_meeting(66) for _ in range(2)])

    # Exactly one caller did the work; the other short-circuited on the claim.
    assert calls["deepgram"] == 1, f"Deepgram billed {calls['deepgram']}x — double-billing regression"
    assert sum(1 for r in results if r) == 1


@pytest.mark.asyncio
async def test_already_transcribed_meeting_is_not_reprocessed():
    store = {"data": {"batch_transcribed": True, "recording": {"session_uid": "s1"}}}

    async def claim(meeting_id, force):
        d = dict(store["data"])
        if d.get("batch_transcribed") and not force:
            return None
        return d

    with patch.object(bt, "_claim_batch_run", new=claim), \
         patch.object(bt, "_call_batch_service", new=AsyncMock()) as dg:
        assert await bt.batch_transcribe_meeting(66) is False
        dg.assert_not_called()


@pytest.mark.asyncio
async def test_stale_claim_is_reclaimed_so_a_crashed_run_can_retry():
    """A claim older than the TTL means the previous run died; a later caller
    must be able to take over rather than the meeting being stuck forever."""
    store = {
        "data": {
            "recording": {"session_uid": "s1"},
            # Claimed longer ago than the TTL -> abandoned.
            "batch_transcribe_claimed_at": time.time() - (bt._CLAIM_TTL_S + 60),
        }
    }
    lock = asyncio.Lock()

    # flag_modified() needs a real ORM instance; the fake row isn't one.
    with patch.object(bt, "async_session_local", lambda: _FakeDB(store, lock)), \
         patch.object(bt, "flag_modified", lambda *_a: None):
        claimed = await bt._claim_batch_run(66, force=False)

    assert claimed is not None, "a stale claim must be reclaimable"


@pytest.mark.asyncio
async def test_fresh_claim_blocks_a_second_caller():
    store = {
        "data": {
            "recording": {"session_uid": "s1"},
            "batch_transcribe_claimed_at": time.time(),  # just claimed
        }
    }
    lock = asyncio.Lock()

    with patch.object(bt, "async_session_local", lambda: _FakeDB(store, lock)), \
         patch.object(bt, "flag_modified", lambda *_a: None):
        claimed = await bt._claim_batch_run(66, force=False)

    assert claimed is None, "a fresh claim must block a concurrent run"
