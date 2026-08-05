"""Tests for the YouTube transcript path.

The json3 fixtures below are the real shapes yt-dlp emits, verified against a live
video's manual and auto caption tracks — in particular the `aAppend` rolling-window
events that auto captions interleave between every real line.
"""

from unittest.mock import AsyncMock, patch

import pytest

from meeting_api import youtube_transcribe as yt


# --- URL parsing -------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    "https://www.youtube.com/watch?v=aircAruvnKk",
    "http://youtube.com/watch?v=aircAruvnKk",
    "https://m.youtube.com/watch?v=aircAruvnKk&list=PLZHQObOWTQDNU6R1_67000Dx_ZCJB-3pi",
    "https://music.youtube.com/watch?v=aircAruvnKk",
    "https://youtu.be/aircAruvnKk",
    "https://youtu.be/aircAruvnKk?t=120",
    "https://www.youtube.com/shorts/aircAruvnKk",
    "https://www.youtube.com/live/aircAruvnKk",
    "https://www.youtube.com/embed/aircAruvnKk",
    "youtube.com/watch?v=aircAruvnKk",
    "  aircAruvnKk  ",
])
def test_extract_video_id_accepts_every_common_form(raw):
    assert yt.extract_video_id(raw) == "aircAruvnKk"


@pytest.mark.parametrize("raw", [
    "https://vimeo.com/123456",
    "https://evil.example.com/watch?v=aircAruvnKk",
    "file:///etc/passwd",
    "ftp://youtube.com/watch?v=aircAruvnKk",
    "https://www.youtube.com/watch?v=tooshort",
    "https://www.youtube.com/watch?v=way_too_long_to_be_an_id",
    "https://www.youtube.com/results?search_query=cats",
    "https://youtu.be/",
    "",
    None,
])
def test_extract_video_id_rejects_everything_else(raw):
    """yt-dlp handles ~1800 sites, so an unchecked URL would make this endpoint a
    fetch-anything primitive. Only YouTube hosts + a real 11-char id get through."""
    with pytest.raises(yt.YouTubeError):
        yt.extract_video_id(raw)


def test_canonical_url_is_what_reaches_yt_dlp():
    """The caller's string is never forwarded — only a rebuilt watch URL."""
    assert yt.canonical_url("aircAruvnKk") == "https://www.youtube.com/watch?v=aircAruvnKk"


# --- Caption parsing ---------------------------------------------------------
def test_parse_json3_manual_track():
    payload = {"events": [
        {"tStartMs": 4220, "dDurationMs": 1180, "segs": [{"utf8": "This is a 3."}]},
        {"tStartMs": 6060, "dDurationMs": 4653, "segs": [
            {"utf8": "It's sloppily written"}, {"utf8": " and rendered badly,"},
        ]},
        # Positioning-only event: no segs, must not become a segment.
        {"tStartMs": 7000, "dDurationMs": 10},
    ]}
    segments = yt.parse_json3_captions(payload)
    assert len(segments) == 2
    assert segments[0] == {"start": 4.22, "end": 5.4, "text": "This is a 3."}
    assert segments[1]["text"] == "It's sloppily written and rendered badly,"


def test_parse_json3_drops_auto_caption_rolling_duplicates():
    """The regression that matters: auto tracks interleave `aAppend` events whose
    only content is a newline, to make the on-screen text scroll. Keeping them
    doubles the segment count and puts a blank line between every sentence."""
    payload = {"events": [
        {"tStartMs": 4400, "dDurationMs": 4159, "segs": [
            {"utf8": "This is a three.", "tOffsetMs": 0, "acAsrConf": 0},
        ]},
        {"tStartMs": 6869, "dDurationMs": 1690, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 6879, "dDurationMs": 4561, "segs": [
            {"utf8": "and rendered at low resolution.", "tOffsetMs": 0},
        ]},
        {"tStartMs": 8549, "dDurationMs": 2891, "aAppend": 1, "segs": [{"utf8": "\n"}]},
    ]}
    segments = yt.parse_json3_captions(payload)
    assert len(segments) == 2, "aAppend rolling-window events leaked through"
    text = yt.assemble_text(segments)
    assert text == "This is a three.\nand rendered at low resolution."
    assert "\n\n" not in text


def test_parse_json3_end_time_falls_back_to_next_start():
    """dDurationMs is absent on some events; end time has to come from somewhere."""
    segments = yt.parse_json3_captions({"events": [
        {"tStartMs": 1000, "segs": [{"utf8": "no duration"}]},
        {"tStartMs": 4000, "dDurationMs": 500, "segs": [{"utf8": "has duration"}]},
    ]})
    assert segments[0]["end"] == 4.0
    assert segments[1]["end"] == 4.5


def test_parse_json3_sorts_and_skips_blank_and_headerless():
    segments = yt.parse_json3_captions({"events": [
        {"tStartMs": 9000, "dDurationMs": 500, "segs": [{"utf8": "second"}]},
        {"tStartMs": 1000, "dDurationMs": 500, "segs": [{"utf8": "first"}]},
        {"tStartMs": 2000, "dDurationMs": 500, "segs": [{"utf8": "   "}]},   # whitespace only
        {"dDurationMs": 500, "segs": [{"utf8": "no start time"}]},          # unanchored
        "not-a-dict",
    ]})
    assert [s["text"] for s in segments] == ["first", "second"]


def test_parse_json3_empty_payload():
    assert yt.parse_json3_captions({}) == []
    assert yt.parse_json3_captions({"events": []}) == []


# --- Track selection ---------------------------------------------------------
def test_manual_captions_beat_auto():
    """Auto captions have no real punctuation model and no speaker turns."""
    meta = {
        "subtitles": {"de": [{"ext": "json3"}]},
        "automatic_captions": {"en": [{"ext": "json3"}]},
    }
    assert yt.choose_caption_track(meta) == ("de", False)


def test_preferred_language_wins_within_a_kind():
    meta = {"subtitles": {"de": [{"ext": "json3"}], "en": [{"ext": "json3"}]}}
    assert yt.choose_caption_track(meta) == ("en", False)


def test_falls_back_to_video_language_then_anything():
    meta = {"subtitles": {"hi": [{"ext": "json3"}], "zz-XX": [{"ext": "json3"}]},
            "language": "hi"}
    assert yt.choose_caption_track(meta) == ("hi", False)
    # No declared language and no preferred match: shortest code wins, so a plain
    # "hi" beats the machine-translated "hi-Latn" variants auto tracks are full of.
    assert yt.choose_caption_track(
        {"automatic_captions": {"hi-Latn": [{"ext": "json3"}], "hi": [{"ext": "json3"}]}}
    ) == ("hi", True)


def test_no_captions_returns_none_so_the_job_falls_back_to_asr():
    assert yt.choose_caption_track({}) is None
    assert yt.choose_caption_track({"subtitles": {}, "automatic_captions": {}}) is None
    # An empty format list is not a usable track.
    assert yt.choose_caption_track({"subtitles": {"en": []}}) is None


# --- ASR fallback ------------------------------------------------------------
@pytest.mark.asyncio
async def test_asr_fallback_maps_speaker_index_to_label(monkeypatch):
    monkeypatch.setenv("TRANSCRIPTION_SERVICE_URL", "http://transcription-service:8000")

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"segments": [
                {"start": 0.0, "end": 1.0, "text": "hello", "speaker_index": 0, "language": "en"},
                {"start": 1.0, "end": 2.0, "text": "  ", "speaker_index": 1},   # blank, dropped
                {"start": 2.0, "end": 3.0, "text": "namaste", "speaker_index": 1, "language": "hi"},
                {"start": 3.0, "end": 4.0, "text": "no index"},                  # speaker stays None
            ]}

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    with patch("meeting_api.youtube_transcribe.httpx.AsyncClient", return_value=_Client()):
        segments = await yt._transcribe_audio_via_service(b"audio", "m4a")

    assert [s["text"] for s in segments] == ["hello", "namaste", "no index"]
    assert [s["speaker"] for s in segments] == ["Speaker 1", "Speaker 2", None]


@pytest.mark.asyncio
async def test_asr_fallback_without_service_url_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("TRANSCRIPTION_SERVICE_URL", raising=False)
    with pytest.raises(yt.YouTubeError, match="TRANSCRIPTION_SERVICE_URL"):
        await yt._transcribe_audio_via_service(b"audio", "m4a")


# --- Job guards --------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_stream_is_refused():
    """A live stream has no final audio to transcribe; yt-dlp would stream forever."""
    with patch.object(yt, "fetch_metadata", new_callable=AsyncMock,
                      return_value={"is_live": True}), \
         patch.object(yt, "_set_status", new_callable=AsyncMock) as set_status, \
         patch.object(yt, "async_session_local") as session:
        session.return_value.__aenter__ = AsyncMock(
            return_value=AsyncMock(get=AsyncMock(return_value=_FakeRow()))
        )
        session.return_value.__aexit__ = AsyncMock(return_value=False)
        ok = await yt.run_job(1)

    assert ok is False
    failed = [c for c in set_status.await_args_list if c.args[1] == "failed"]
    assert failed and "live stream" in failed[-1].kwargs["error"].lower()


@pytest.mark.asyncio
async def test_over_long_video_is_refused_before_any_download(monkeypatch):
    monkeypatch.setattr(yt, "MAX_DURATION_SECONDS", 600)
    with patch.object(yt, "fetch_metadata", new_callable=AsyncMock,
                      return_value={"duration": 7200, "title": "long"}), \
         patch.object(yt, "fetch_audio", new_callable=AsyncMock) as fetch_audio, \
         patch.object(yt, "_set_status", new_callable=AsyncMock) as set_status, \
         patch.object(yt, "async_session_local") as session:
        session.return_value.__aenter__ = AsyncMock(
            return_value=AsyncMock(get=AsyncMock(return_value=_FakeRow()))
        )
        session.return_value.__aexit__ = AsyncMock(return_value=False)
        ok = await yt.run_job(1)

    assert ok is False
    fetch_audio.assert_not_awaited()  # the point: refuse before spending bandwidth
    failed = [c for c in set_status.await_args_list if c.args[1] == "failed"]
    assert failed and "limit" in failed[-1].kwargs["error"]


class _FakeRow:
    id = 1
    video_id = "aircAruvnKk"
    user_id = 7
    status = "queued"
