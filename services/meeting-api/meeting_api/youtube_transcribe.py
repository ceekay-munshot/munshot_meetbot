"""Transcribe a public YouTube video, owned by a Vexa user.

Two-stage strategy, cheapest first:

  1. CAPTIONS — ask yt-dlp for the video's own subtitles (manual track preferred,
     auto-generated as second choice) in YouTube's `json3` format. Free, no ASR
     spend, near-instant. Gives no speaker labels.
  2. DEEPGRAM — only when the video has no usable caption track: download the
     audio and POST it to the SAME transcription-service batch endpoint the
     post-meeting path uses (`/v1/transcribe/batch`, nova-3, language='multi',
     diarization on). Costs pre-recorded minutes but works on any video and
     yields speaker labels.

Jobs run in the background (the POST returns an id immediately) under a
semaphore, because this competes with meeting bots for a 4-core box: a bot needs
~0.6 core sustained and yt-dlp + ffmpeg will happily eat a whole core. See
YOUTUBE_MAX_CONCURRENT_JOBS.

yt-dlp is invoked as a SUBPROCESS rather than imported: a wedged or crashing
extractor (YouTube changes them often) then cannot take meeting-api down with it,
and the audio never has to pass through this process's heap on its way to disk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import delete, select

from .database import async_session_local
from .models import YouTubeTranscript, YouTubeTranscriptSegment

logger = logging.getLogger("meeting_api.youtube_transcribe")

# --- Config ------------------------------------------------------------------
YT_DLP_BIN = os.getenv("YT_DLP_BIN", "yt-dlp")
# Cap concurrent jobs: each one is a yt-dlp download plus (on the ASR path) an
# ffmpeg extract, on the same host as the meeting bots.
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("YOUTUBE_MAX_CONCURRENT_JOBS", "1")))
# Refuse absurdly long videos outright — a 10-hour stream is hours of ASR spend
# and hundreds of MB through a 1 GiB container.
MAX_DURATION_SECONDS = int(os.getenv("YOUTUBE_MAX_DURATION_SECONDS", str(4 * 3600)))
# Preference order for the caption track. 'en' first, then the video's own
# language, then anything — overridable for an all-Hindi corpus, say.
CAPTION_LANGS = [
    s.strip() for s in os.getenv("YOUTUBE_CAPTION_LANGS", "en,en-US,en-GB").split(",") if s.strip()
]
METADATA_TIMEOUT_S = float(os.getenv("YOUTUBE_METADATA_TIMEOUT_SECONDS", "60"))
CAPTION_TIMEOUT_S = float(os.getenv("YOUTUBE_CAPTION_TIMEOUT_SECONDS", "120"))
AUDIO_TIMEOUT_S = float(os.getenv("YOUTUBE_AUDIO_TIMEOUT_SECONDS", "900"))
BATCH_TIMEOUT_S = float(os.getenv("BATCH_TRANSCRIBE_TIMEOUT_SECONDS", "600"))

_job_semaphore: Optional[asyncio.Semaphore] = None


def _semaphore() -> asyncio.Semaphore:
    """Lazily built so the semaphore binds to the running loop, not import time."""
    global _job_semaphore
    if _job_semaphore is None:
        _job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)
    return _job_semaphore


class YouTubeError(Exception):
    """A job failed in a way worth reporting back to the caller verbatim."""


# --- URL handling ------------------------------------------------------------
# An 11-char base64url id. Anchored, so nothing else can ride along.
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be",
}


def extract_video_id(raw: str) -> str:
    """Pull the 11-char video id out of any common YouTube URL form.

    Accepts watch?v=, youtu.be/<id>, /shorts/<id>, /live/<id>, /embed/<id>, or a
    bare id. Raises YouTubeError on anything else.

    The id — never the caller's string — is what gets handed to yt-dlp, and it is
    re-canonicalised into a youtube.com/watch URL first. That matters: yt-dlp
    accepts arbitrary URLs for ~1800 sites, so passing user input through
    unchecked would turn this endpoint into a fetch-anything primitive (SSRF, and
    a way to bill our ASR against someone else's media host).
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise YouTubeError("No YouTube URL provided")

    if _VIDEO_ID_RE.match(candidate):
        return candidate

    from urllib.parse import parse_qs, urlparse

    if "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise YouTubeError(f"Unsupported URL scheme: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if host not in _YOUTUBE_HOSTS:
        raise YouTubeError(f"Not a YouTube URL (host {host!r})")

    # youtu.be/<id> and /shorts/<id> | /live/<id> | /embed/<id> | /v/<id>
    path_parts = [p for p in (parsed.path or "").split("/") if p]
    if host in ("youtu.be", "www.youtu.be"):
        if path_parts and _VIDEO_ID_RE.match(path_parts[0]):
            return path_parts[0]
        raise YouTubeError("Could not find a video id in the youtu.be URL")
    if len(path_parts) >= 2 and path_parts[0] in ("shorts", "live", "embed", "v"):
        if _VIDEO_ID_RE.match(path_parts[1]):
            return path_parts[1]
        raise YouTubeError(f"Invalid video id in /{path_parts[0]}/ URL")

    vid = (parse_qs(parsed.query or "").get("v") or [None])[0]
    if vid and _VIDEO_ID_RE.match(vid):
        return vid
    raise YouTubeError("Could not find a video id in the URL (expected ?v=<id>)")


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


# --- yt-dlp plumbing ---------------------------------------------------------
async def _run(args: List[str], timeout: float, cwd: Optional[str] = None) -> Tuple[int, bytes, bytes]:
    """Run yt-dlp, killing it on timeout so a wedged extractor can't hold a slot."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise YouTubeError(f"yt-dlp timed out after {timeout:.0f}s")
    return proc.returncode or 0, stdout, stderr


def _yt_dlp_available() -> bool:
    return shutil.which(YT_DLP_BIN) is not None


def _stderr_reason(stderr: bytes) -> str:
    """Surface yt-dlp's own ERROR line — it says 'Private video', 'Video
    unavailable', 'Sign in to confirm your age' etc., which is exactly what the
    caller needs to hear."""
    for line in (stderr or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if line.startswith("ERROR:"):
            return line[len("ERROR:"):].strip()
    return "yt-dlp failed (no ERROR line in output)"


async def fetch_metadata(video_id: str) -> Dict[str, Any]:
    """Video metadata + the map of available caption tracks, in one call."""
    if not _yt_dlp_available():
        raise YouTubeError(
            f"{YT_DLP_BIN} is not installed in this image — cannot fetch YouTube videos"
        )
    code, stdout, stderr = await _run(
        [YT_DLP_BIN, "--skip-download", "--dump-single-json", "--no-warnings",
         "--no-playlist", canonical_url(video_id)],
        timeout=METADATA_TIMEOUT_S,
    )
    if code != 0:
        raise YouTubeError(_stderr_reason(stderr))
    try:
        return json.loads(stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise YouTubeError(f"Could not parse yt-dlp metadata: {e}")


def choose_caption_track(meta: Dict[str, Any]) -> Optional[Tuple[str, bool]]:
    """Pick (language, is_automatic) for the best available caption track.

    Manual subtitles beat auto-generated ones: auto captions have no punctuation
    model worth the name and no speaker turns. Within each kind, CAPTION_LANGS
    order wins, then the video's declared language, then whatever exists.
    Returns None when the video has no caption track at all.
    """
    manual = {k: v for k, v in (meta.get("subtitles") or {}).items() if v}
    auto = {k: v for k, v in (meta.get("automatic_captions") or {}).items() if v}
    video_lang = (meta.get("language") or "").strip()

    for tracks, is_auto in ((manual, False), (auto, True)):
        if not tracks:
            continue
        preference = CAPTION_LANGS + ([video_lang] if video_lang else [])
        for lang in preference:
            if lang in tracks:
                return lang, is_auto
        # Any language beats no transcript; prefer a plain 2-letter code over the
        # machine-translated "en-xx" variants auto captions are littered with.
        for lang in sorted(tracks, key=lambda s: (len(s), s)):
            return lang, is_auto
    return None


def parse_json3_captions(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Turn YouTube's json3 caption payload into ordered transcript segments.

    json3 is `{"events": [{"tStartMs", "dDurationMs", "segs": [{"utf8"}]}]}`, with
    two traps on the auto-generated tracks:

      * Rolling-window duplicates. Auto captions emit an `aAppend: 1` event
        between every real line whose only content is "\\n" — they exist to make
        the on-screen text scroll. Concatenating them yields a transcript with a
        blank line between every sentence, and doubles the segment count.
      * `dDurationMs` is absent on some events, so end time has to fall back to
        the next event's start.

    Verified against both the manual and auto `json3` tracks of a real video.
    """
    events = payload.get("events") or []
    raw: List[Tuple[float, Optional[float], str]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("aAppend"):
            continue  # rolling-window continuation, not a real line
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(
            seg.get("utf8", "") for seg in segs if isinstance(seg, dict)
        ).strip()
        if not text:
            continue
        start_ms = event.get("tStartMs")
        if start_ms is None:
            continue
        dur_ms = event.get("dDurationMs")
        raw.append((float(start_ms), float(dur_ms) if dur_ms is not None else None, text))

    raw.sort(key=lambda r: r[0])
    segments: List[Dict[str, Any]] = []
    for i, (start_ms, dur_ms, text) in enumerate(raw):
        if dur_ms is not None:
            end_ms = start_ms + dur_ms
        elif i + 1 < len(raw):
            end_ms = raw[i + 1][0]
        else:
            end_ms = start_ms + 2000.0
        segments.append({
            "start": start_ms / 1000.0,
            "end": max(end_ms, start_ms) / 1000.0,
            "text": text,
        })
    return segments


async def fetch_captions(video_id: str, lang: str, is_auto: bool) -> List[Dict[str, Any]]:
    """Download one caption track and parse it into segments."""
    flag = "--write-auto-subs" if is_auto else "--write-subs"
    with tempfile.TemporaryDirectory(prefix="yt-caps-") as tmp:
        code, _stdout, stderr = await _run(
            [YT_DLP_BIN, "--skip-download", flag, "--sub-langs", lang,
             "--sub-format", "json3", "--no-warnings", "--no-playlist",
             "-o", "cap.%(ext)s", canonical_url(video_id)],
            timeout=CAPTION_TIMEOUT_S,
            cwd=tmp,
        )
        if code != 0:
            raise YouTubeError(_stderr_reason(stderr))
        files = sorted(Path(tmp).glob("*.json3"))
        if not files:
            raise YouTubeError(f"yt-dlp wrote no caption file for language {lang!r}")
        try:
            payload = json.loads(files[0].read_text(encoding="utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            raise YouTubeError(f"Could not parse caption file: {e}")
    return parse_json3_captions(payload)


async def fetch_audio(video_id: str, tmpdir: str) -> Tuple[bytes, str]:
    """Download bestaudio to disk and read it back. Returns (bytes, format).

    Straight to disk rather than through a pipe: the ASR call needs the whole blob
    in memory anyway, but this way the download itself does not also buffer in the
    heap, and yt-dlp's own retry/resume logic still applies.
    """
    code, _stdout, stderr = await _run(
        [YT_DLP_BIN, "-f", "bestaudio/best", "--extract-audio", "--audio-format", "m4a",
         "--no-warnings", "--no-playlist", "-o", "audio.%(ext)s", canonical_url(video_id)],
        timeout=AUDIO_TIMEOUT_S,
        cwd=tmpdir,
    )
    if code != 0:
        raise YouTubeError(_stderr_reason(stderr))
    files = [p for p in sorted(Path(tmpdir).iterdir()) if p.is_file()]
    if not files:
        raise YouTubeError("yt-dlp produced no audio file")
    audio = max(files, key=lambda p: p.stat().st_size)
    return audio.read_bytes(), audio.suffix.lstrip(".") or "m4a"


async def _transcribe_audio_via_service(audio: bytes, fmt: str) -> List[Dict[str, Any]]:
    """POST the audio to the same batch endpoint the post-meeting path uses."""
    base = (os.getenv("TRANSCRIPTION_SERVICE_URL", "") or "").strip().rstrip("/")
    if base.endswith("/v1/audio/transcriptions"):
        base = base[: -len("/v1/audio/transcriptions")]
    if not base:
        raise YouTubeError("TRANSCRIPTION_SERVICE_URL not set; cannot run ASR fallback")
    token = (os.getenv("TRANSCRIPTION_SERVICE_TOKEN", "") or "").strip()
    headers = {"X-API-Key": token} if token else {}

    timeout = httpx.Timeout(BATCH_TIMEOUT_S, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{base}/v1/transcribe/batch",
            headers=headers,
            data={"diarize": "true"},
            files={"file": (f"youtube.{fmt}", audio, f"audio/{fmt}")},
        )
    if resp.status_code != 200:
        raise YouTubeError(
            f"transcription service returned {resp.status_code}: {resp.text[:300]}"
        )
    result = resp.json()
    segments = []
    for seg in result.get("segments") or []:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        idx = seg.get("speaker_index")
        segments.append({
            "start": float(seg.get("start", 0.0)),
            "end": float(seg.get("end", 0.0)),
            "text": text,
            "speaker": f"Speaker {int(idx) + 1}" if idx is not None else None,
            "language": seg.get("language"),
        })
    return segments


def assemble_text(segments: List[Dict[str, Any]]) -> str:
    """Flatten segments into the plain transcript the frontend renders."""
    return "\n".join(s["text"] for s in segments if s.get("text"))


# --- Job runner --------------------------------------------------------------
async def _set_status(
    transcript_id: int, status: str, *, error: Optional[str] = None, **fields: Any
) -> None:
    async with async_session_local() as db:
        row = await db.get(YouTubeTranscript, transcript_id)
        if not row:
            return
        row.status = status
        if error is not None:
            row.error = error[:2000]
        for key, value in fields.items():
            setattr(row, key, value)
        if status in ("completed", "failed"):
            row.completed_at = datetime.utcnow()
        await db.commit()


async def run_job(transcript_id: int) -> bool:
    """Fetch, transcribe and store one video. Never raises — records `failed`.

    Returns True when segments were written.
    """
    async with _semaphore():
        try:
            return await _run_job_inner(transcript_id)
        except YouTubeError as e:
            logger.warning(f"youtube: transcript {transcript_id} failed: {e}")
            await _set_status(transcript_id, "failed", error=str(e))
            return False
        except Exception as e:  # noqa: BLE001 - a background job must not vanish silently
            logger.error(
                f"youtube: transcript {transcript_id} crashed: {type(e).__name__}: {e}",
                exc_info=True,
            )
            await _set_status(transcript_id, "failed", error=f"{type(e).__name__}: {e}")
            return False


async def _run_job_inner(transcript_id: int) -> bool:
    async with async_session_local() as db:
        row = await db.get(YouTubeTranscript, transcript_id)
        if not row:
            logger.warning(f"youtube: transcript {transcript_id} disappeared before its job ran")
            return False
        if row.status == "completed":
            return False  # idempotent: a re-queued job is a no-op
        video_id, user_id = row.video_id, row.user_id

    await _set_status(transcript_id, "processing")

    meta = await fetch_metadata(video_id)
    if meta.get("is_live"):
        raise YouTubeError("Video is a live stream; wait until it has ended")
    duration = meta.get("duration")
    if duration and int(duration) > MAX_DURATION_SECONDS:
        raise YouTubeError(
            f"Video is {int(duration) // 60} min, over the "
            f"{MAX_DURATION_SECONDS // 60} min limit (YOUTUBE_MAX_DURATION_SECONDS)"
        )
    await _set_status(
        transcript_id, "processing",
        title=meta.get("title"),
        channel=meta.get("channel") or meta.get("uploader"),
        duration_seconds=int(duration) if duration else None,
    )

    # 1. Captions.
    segments: List[Dict[str, Any]] = []
    source = language = None
    track = choose_caption_track(meta)
    if track:
        lang, is_auto = track
        try:
            segments = await fetch_captions(video_id, lang, is_auto)
            if segments:
                source, language = "captions", lang
                logger.info(
                    f"youtube: transcript {transcript_id} used "
                    f"{'auto' if is_auto else 'manual'} {lang} captions ({len(segments)} segments)"
                )
        except YouTubeError as e:
            # Not fatal — fall through to ASR, which is the whole point of having it.
            logger.warning(
                f"youtube: transcript {transcript_id} caption fetch failed ({e}); falling back to ASR"
            )

    # 2. ASR fallback.
    if not segments:
        logger.info(f"youtube: transcript {transcript_id} has no usable captions; downloading audio for ASR")
        with tempfile.TemporaryDirectory(prefix="yt-audio-") as tmp:
            audio, fmt = await fetch_audio(video_id, tmp)
        logger.info(f"youtube: transcript {transcript_id} sending {len(audio)} bytes to batch ASR")
        segments = await _transcribe_audio_via_service(audio, fmt)
        del audio  # release before the D1 mirror allocates
        source = "deepgram"
        language = next((s.get("language") for s in segments if s.get("language")), None)

    if not segments:
        raise YouTubeError("No transcript could be produced (no captions and ASR returned nothing)")

    # 3. Store. Replace wholesale so a retry can't leave a half-old transcript.
    async with async_session_local() as db:
        await db.execute(
            delete(YouTubeTranscriptSegment).where(
                YouTubeTranscriptSegment.transcript_id == transcript_id
            )
        )
        for i, seg in enumerate(segments):
            db.add(YouTubeTranscriptSegment(
                transcript_id=transcript_id,
                segment_index=i,
                start_time=seg["start"],
                end_time=seg["end"],
                text=seg["text"],
                speaker=seg.get("speaker"),
                language=seg.get("language") or language,
            ))
        await db.commit()

    await _set_status(
        transcript_id, "completed",
        source=source, language=language, segment_count=len(segments), error=None,
    )
    logger.info(
        f"youtube: transcript {transcript_id} completed via {source} ({len(segments)} segments)"
    )

    # 4. Best-effort mirror for the Cloudflare frontend.
    try:
        from .collector.d1_youtube_forwarder import safe_mirror_youtube_transcript
        await safe_mirror_youtube_transcript(transcript_id)
    except Exception as e:  # noqa: BLE001 - mirror must never fail the job
        logger.error(f"youtube: D1 mirror failed for {transcript_id} (non-fatal): {e}")
    return True


def spawn_job(transcript_id: int) -> None:
    """Fire-and-forget the job, keeping a reference so it isn't GC'd mid-flight."""
    task = asyncio.create_task(run_job(transcript_id), name=f"youtube-job-{transcript_id}")
    _running.add(task)
    task.add_done_callback(_running.discard)


_running: set = set()
