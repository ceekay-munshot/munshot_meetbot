"""Regression tests for the audio-retention sweep and the storage layout it prunes.

The sweep shipped dead: its candidate query required `data->'recording'` to exist,
but that key is only written when a chunk arrives with is_final=true, and the bot
dropped that chunk whenever MediaRecorder emitted it empty. So the key was absent
on every meeting, the query matched nothing, and MinIO grew without bound while
AUDIO_RETENTION_DAYS looked configured. These tests pin the behaviour that fixes
it: retention must not depend on a bot-side success signal.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meeting_api import recording_store
from meeting_api.schemas import MeetingStatus
from meeting_api.sweeps import _sweep_audio_retention

from .conftest import make_meeting


class _DbContext:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RowsResult:
    """Minimal stand-in for a Core result (the sweep uses .fetchall())."""

    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


@pytest.mark.asyncio
async def test_purges_meeting_with_no_recording_key(mock_db):
    """The regression: a meeting whose bot never sent is_final still gets swept.

    `data` here has no 'recording' key at all — the exact shape of all 135 rows on
    the live box. The pre-fix query skipped these, so their audio was immortal.
    """
    old = datetime.utcnow() - timedelta(days=30)
    meeting = make_meeting(
        id=40, status=MeetingStatus.COMPLETED.value, created_at=old, data={},
    )
    mock_db.execute = AsyncMock(return_value=_RowsResult([(40,)]))
    mock_db.get = AsyncMock(return_value=meeting)

    with patch.object(
        recording_store, "delete_meeting_audio", new_callable=AsyncMock, return_value=213,
    ) as del_audio, patch.object(
        recording_store, "delete_meeting_screenshots", new_callable=AsyncMock, return_value=0,
    ), patch("meeting_api.sweeps.flag_modified"):
        swept = await _sweep_audio_retention(lambda: _DbContext(mock_db))

    assert swept == 1
    del_audio.assert_awaited_once_with(40)
    assert meeting.data["recording"]["audio_deleted_at"]
    assert meeting.data["recording"]["audio_chunks_deleted"] == 213


@pytest.mark.asyncio
async def test_candidate_query_does_not_require_recording_key(mock_db):
    """Guard the WHERE clause itself, so the dead-code regression can't come back."""
    mock_db.execute = AsyncMock(return_value=_RowsResult([]))
    await _sweep_audio_retention(lambda: _DbContext(mock_db))

    sql = str(mock_db.execute.await_args.args[0])
    assert "data->'recording' IS NOT NULL" not in sql
    assert "audio_deleted_at' IS NULL" in sql
    assert "audio_retention_checked_at' IS NULL" in sql


@pytest.mark.asyncio
async def test_meeting_with_no_stored_media_is_marked_but_not_reported_as_deleted(mock_db):
    """A meeting that never recorded must not claim its audio "expired".

    /audio renders audio_deleted_at as "deleted per retention policy"; setting it
    on a meeting with nothing stored would report a purge that never happened. It
    still needs marking, or every iteration rescans it forever.
    """
    old = datetime.utcnow() - timedelta(days=30)
    meeting = make_meeting(
        id=41, status=MeetingStatus.FAILED.value, created_at=old, data={},
    )
    mock_db.execute = AsyncMock(return_value=_RowsResult([(41,)]))
    mock_db.get = AsyncMock(return_value=meeting)

    with patch.object(
        recording_store, "delete_meeting_audio", new_callable=AsyncMock, return_value=0,
    ), patch.object(
        recording_store, "delete_meeting_screenshots", new_callable=AsyncMock, return_value=0,
    ), patch("meeting_api.sweeps.flag_modified"):
        swept = await _sweep_audio_retention(lambda: _DbContext(mock_db))

    assert swept == 0  # nothing purged, so nothing to report
    rec = meeting.data["recording"]
    assert rec["audio_retention_checked_at"]
    assert "audio_deleted_at" not in rec


@pytest.mark.asyncio
async def test_speaker_timeline_samples_pruned_with_the_audio(mock_db):
    """The timeline only maps segments onto audio; once that's gone it's dead weight.

    It is the largest JSONB key by far (~2 samples/second for the whole meeting —
    7.9 MB of an 11 MB table on the live box).
    """
    old = datetime.utcnow() - timedelta(days=30)
    meeting = make_meeting(
        id=42, status=MeetingStatus.COMPLETED.value, created_at=old,
        data={"speaker_timeline": {
            "session_uid": "s1",
            "samples": [{"t_ms": i * 500, "speaking": ["Ann"]} for i in range(1000)],
        }},
    )
    mock_db.execute = AsyncMock(return_value=_RowsResult([(42,)]))
    mock_db.get = AsyncMock(return_value=meeting)

    with patch.object(
        recording_store, "delete_meeting_audio", new_callable=AsyncMock, return_value=5,
    ), patch.object(
        recording_store, "delete_meeting_screenshots", new_callable=AsyncMock, return_value=8,
    ), patch("meeting_api.sweeps.flag_modified"):
        await _sweep_audio_retention(lambda: _DbContext(mock_db))

    timeline = meeting.data["speaker_timeline"]
    assert timeline["samples"] == []
    assert timeline["samples_pruned"] == 1000
    assert timeline["session_uid"] == "s1"  # identity kept for forensics
    assert meeting.data["recording"]["screenshots_deleted"] == 8


# --- storage layout guards ---------------------------------------------------

def test_chunk_key_shape_excludes_legacy_layout():
    """recordings/{meeting_id}/ is shared with a pre-0.10.5 per-user layout.

    Legacy keys were recordings/{user_id}/{recording_id}/{session}/audio/{seq}.webm,
    so sweeping meeting 1 would have listed and deleted user 1's tree.
    """
    assert recording_store._is_chunk_key("recordings/1/sess-a/000000.webm", 1)
    assert not recording_store._is_chunk_key(
        "recordings/1/900902341514/sess-a/audio/000003.webm", 1
    )
    assert not recording_store._is_chunk_key("recordings/12/sess-a/000000.webm", 1)
    assert not recording_store._is_chunk_key("meeting-screenshots/1/shot.png", 1)


def test_dominant_session_picks_the_one_holding_the_audio():
    """Session uids are random UUIDs, so "lexically first" picked arbitrarily —
    a 3-second reconnect stub could beat the real 90-minute recording."""
    objects = [
        ("recordings/7/aaaa/000000.webm", 2_000),        # lexically first, tiny
        ("recordings/7/zzzz/000000.webm", 5_000_000),
        ("recordings/7/zzzz/000001.webm", 5_000_000),
    ]
    assert recording_store._dominant_session(objects) == "zzzz"
    assert recording_store._dominant_session([]) is None
