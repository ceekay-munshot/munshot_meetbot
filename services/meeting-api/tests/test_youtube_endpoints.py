"""Router-level tests for the YouTube transcript endpoints.

Mounts the real router on a bare app and overrides only auth + the DB session, so
route ordering, ownership checks and the queue/dedup logic are exercised for real.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from meeting_api.auth import UserProxy
from meeting_api.collector.auth import get_current_user
from meeting_api.database import get_db
from meeting_api.models import YouTubeTranscript, YouTubeTranscriptSegment
from meeting_api.youtube import router as youtube_router

OWNER_ID = 7
OTHER_ID = 99


def _row(**overrides) -> MagicMock:
    defaults = dict(
        id=5, user_id=OWNER_ID, video_id="aircAruvnKk",
        url="https://www.youtube.com/watch?v=aircAruvnKk",
        title="A talk", channel="Founders", duration_seconds=1120,
        status="completed", source="captions", language="en", segment_count=2,
        error=None, created_at=datetime(2026, 7, 30, 12, 0, 0), completed_at=None,
    )
    defaults.update(overrides)
    row = MagicMock(spec=YouTubeTranscript)
    for k, v in defaults.items():
        setattr(row, k, v)
    return row


def _segment(index: int, text: str, speaker=None) -> MagicMock:
    seg = MagicMock(spec=YouTubeTranscriptSegment)
    seg.segment_index, seg.text, seg.speaker = index, text, speaker
    seg.start_time, seg.end_time, seg.language = float(index), float(index) + 1.0, "en"
    return seg


class _Result:
    def __init__(self, items):
        self._items = items

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakeDb:
    """Serves a scripted queue of execute() results; records add/commit."""

    def __init__(self, get_result=None, execute_results=None):
        self._get_result = get_result
        self._execute_results = list(execute_results or [])
        self.added = []
        self.commits = 0

    async def get(self, _model, _pk):
        return self._get_result

    async def execute(self, *_a, **_k):
        return self._execute_results.pop(0) if self._execute_results else _Result([])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        # Stand in for the DB assigning the serial primary key.
        if getattr(obj, "id", None) is None:
            obj.id = 5


def _client(db: _FakeDb, user_id: int = OWNER_ID) -> TestClient:
    app = FastAPI()
    app.include_router(youtube_router)
    app.dependency_overrides[get_current_user] = lambda: UserProxy(user_id, 2, ["bot"])
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


# --- POST /youtube/transcripts ----------------------------------------------
def test_post_queues_a_new_video():
    db = _FakeDb(execute_results=[_Result([])])  # no existing transcript
    with patch("meeting_api.youtube_transcribe.spawn_job") as spawn:
        resp = _client(db).post(
            "/youtube/transcripts",
            json={"url": "https://youtu.be/aircAruvnKk?t=30"},
        )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["video_id"] == "aircAruvnKk"
    # The canonical URL is stored, not the caller's string.
    assert body["url"] == "https://www.youtube.com/watch?v=aircAruvnKk"
    assert len(db.added) == 1
    spawn.assert_called_once_with(5)


def test_post_is_idempotent_per_user_and_video():
    """Re-pasting a link the user already transcribed must not re-run the job
    (and on the ASR path, must not re-bill Deepgram for the same video)."""
    existing = _row(id=42, status="completed")
    db = _FakeDb(execute_results=[_Result([existing])])
    with patch("meeting_api.youtube_transcribe.spawn_job") as spawn:
        resp = _client(db).post(
            "/youtube/transcripts", json={"url": "https://www.youtube.com/watch?v=aircAruvnKk"},
        )
    assert resp.status_code == 202
    assert resp.json()["id"] == 42
    spawn.assert_not_called()
    assert db.added == []


def test_post_retries_a_previously_failed_video():
    """A transient YouTube error must not wedge that video forever."""
    existing = _row(id=42, status="failed", error="Video unavailable")
    db = _FakeDb(execute_results=[_Result([existing])])
    with patch("meeting_api.youtube_transcribe.spawn_job") as spawn:
        resp = _client(db).post(
            "/youtube/transcripts", json={"url": "https://www.youtube.com/watch?v=aircAruvnKk"},
        )
    assert resp.status_code == 202
    assert resp.json()["status"] == "queued"
    assert existing.error is None
    spawn.assert_called_once_with(42)


def test_post_force_requeues_a_completed_video():
    existing = _row(id=42, status="completed")
    db = _FakeDb(execute_results=[_Result([existing])])
    with patch("meeting_api.youtube_transcribe.spawn_job") as spawn:
        resp = _client(db).post(
            "/youtube/transcripts",
            json={"url": "https://www.youtube.com/watch?v=aircAruvnKk", "force": True},
        )
    assert resp.status_code == 202
    spawn.assert_called_once_with(42)


def test_post_rejects_a_non_youtube_url():
    db = _FakeDb()
    with patch("meeting_api.youtube_transcribe.spawn_job") as spawn:
        resp = _client(db).post("/youtube/transcripts", json={"url": "https://vimeo.com/12345"})
    assert resp.status_code == 422
    assert "YouTube" in resp.json()["detail"]
    spawn.assert_not_called()


# --- GET /youtube/transcripts/{id} ------------------------------------------
def test_get_returns_segments_and_assembled_text():
    db = _FakeDb(
        get_result=_row(),
        execute_results=[_Result([_segment(0, "Hello there"), _segment(1, "Second line")])],
    )
    resp = _client(db).get("/youtube/transcripts/5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "Hello there\nSecond line"
    assert [s["index"] for s in body["segments"]] == [0, 1]
    assert body["source"] == "captions"


def test_get_another_users_transcript_is_404_not_403():
    """A 403 would confirm the id exists."""
    db = _FakeDb(get_result=_row(user_id=OTHER_ID))
    resp = _client(db).get("/youtube/transcripts/5")
    assert resp.status_code == 404


def test_get_missing_transcript_is_404():
    resp = _client(_FakeDb(get_result=None)).get("/youtube/transcripts/5")
    assert resp.status_code == 404


# --- GET /youtube/transcripts/{id}.txt --------------------------------------
def test_txt_route_resolves_and_is_not_swallowed_by_the_int_route():
    """Regression: with the int-typed route declared first, FastAPI tries
    int("5.txt") and 422s before this handler is ever reached."""
    db = _FakeDb(
        get_result=_row(),
        execute_results=[_Result([_segment(0, "Hello there"), _segment(1, "Second line")])],
    )
    resp = _client(db).get("/youtube/transcripts/5.txt")
    assert resp.status_code == 200, f"expected text, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"].startswith("text/plain")
    assert resp.text == "Hello there\nSecond line"


def test_txt_route_conflicts_while_still_processing():
    db = _FakeDb(get_result=_row(status="processing"))
    resp = _client(db).get("/youtube/transcripts/5.txt")
    assert resp.status_code == 409
    assert "processing" in resp.json()["detail"]


def test_txt_route_reports_the_failure_reason():
    db = _FakeDb(get_result=_row(status="failed", error="Private video"))
    resp = _client(db).get("/youtube/transcripts/5.txt")
    assert resp.status_code == 409
    assert "Private video" in resp.json()["detail"]


# --- GET /youtube/transcripts ----------------------------------------------
def test_list_returns_metadata_without_segments():
    db = _FakeDb(execute_results=[_Result([_row(id=1), _row(id=2)])])
    resp = _client(db).get("/youtube/transcripts")
    assert resp.status_code == 200
    body = resp.json()
    assert [t["id"] for t in body] == [1, 2]
    assert all(t["segments"] == [] for t in body), "list must stay cheap"
