"""MinIO/S3 storage for raw meeting-audio chunks (post-meeting batch path).

The bot's RecordingService POSTs one audio chunk per MediaRecorder timeslice to
``POST /internal/recordings/chunk``; each chunk lands here immediately so an
ungraceful bot exit still leaves the already-uploaded chunks durable. After the
meeting ends, the post-meeting batch job (``batch_transcribe.py``) lists +
assembles every chunk into one audio blob and sends it to Deepgram once.

Storage layout (one object per chunk, zero-padded seq keeps lexical = numeric
order):

    recordings/{meeting_id}/{session_uid}/{chunk_seq:06d}.{format}

boto3 is synchronous; every call here is wrapped in ``asyncio.to_thread`` by the
callers so the meeting-api event loop never blocks on S3 I/O.
"""
from __future__ import annotations

import os
import asyncio
import logging
from typing import List, Optional, Tuple

import boto3
from botocore.config import Config as BotoConfig

logger = logging.getLogger("meeting_api.recording_store")


def _bucket() -> str:
    return os.environ.get("MINIO_BUCKET", "vexa-recordings")


def _client():
    """Build a MinIO/S3 client (mirrors the config used elsewhere in meetings.py)."""
    minio_endpoint = os.environ.get("MINIO_ENDPOINT", "minio:9000")
    minio_secure = os.environ.get("MINIO_SECURE", "false").lower() == "true"
    s3_endpoint = f"{'https' if minio_secure else 'http'}://{minio_endpoint}"
    return boto3.client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=os.environ.get("MINIO_ACCESS_KEY", ""),
        aws_secret_access_key=os.environ.get("MINIO_SECRET_KEY", ""),
        config=BotoConfig(signature_version="s3v4"),
        region_name="us-east-1",
    )


def chunk_key(meeting_id: int, session_uid: str, chunk_seq: int, fmt: str) -> str:
    return f"recordings/{meeting_id}/{session_uid}/{chunk_seq:06d}.{fmt}"


def _prefix(meeting_id: int, session_uid: Optional[str] = None) -> str:
    if session_uid:
        return f"recordings/{meeting_id}/{session_uid}/"
    return f"recordings/{meeting_id}/"


def screenshots_prefix(meeting_id: int) -> str:
    """Where the bot uploads its join/admission diagnostic screenshots on failure
    (see vexa-bot core/src/s3-sync.ts uploadFailureScreenshots)."""
    return f"meeting-screenshots/{meeting_id}/"


def _is_chunk_key(key: str, meeting_id: int) -> bool:
    """True only for keys in the CURRENT layout: recordings/{meeting_id}/{session}/{seq}.{fmt}.

    A pre-0.10.5 layout wrote recordings/{user_id}/{recording_id}/{session}/audio/{seq}.{fmt},
    which shares the `recordings/<int>/` prefix with this meeting's namespace. Without
    this shape check, listing or deleting meeting N would also reach user N's legacy
    tree — assembling foreign audio into a transcript, or deleting it. Nothing in the
    live bucket uses the legacy layout any more; this keeps it that way by construction.
    """
    parts = key.split("/")
    return len(parts) == 4 and parts[0] == "recordings" and parts[1] == str(meeting_id)


def _ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except Exception:
        try:
            s3.create_bucket(Bucket=bucket)
            logger.info(f"recording_store: created bucket {bucket}")
        except Exception as e:
            # Race or already-exists — head again, ignore if present.
            logger.debug(f"recording_store: create_bucket noop ({e})")


# --- Sync workers (run via asyncio.to_thread) --------------------------------
def _put_chunk_sync(meeting_id: int, session_uid: str, chunk_seq: int, fmt: str, data: bytes) -> str:
    s3 = _client()
    bucket = _bucket()
    _ensure_bucket(s3, bucket)
    key = chunk_key(meeting_id, session_uid, chunk_seq, fmt)
    s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=f"audio/{fmt}")
    return key


def _list_objects_sync(meeting_id: int, session_uid: Optional[str]) -> List[Tuple[str, int]]:
    """(key, size) for every chunk of this meeting, sorted by key.

    Lexical sort == numeric because seq is zero-padded; if multiple sessions are
    present (session_uid=None), they group by session then by seq.
    """
    s3 = _client()
    bucket = _bucket()
    objects: List[Tuple[str, int]] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=_prefix(meeting_id, session_uid)):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            if _is_chunk_key(key, meeting_id):
                objects.append((key, int(obj.get("Size") or 0)))
    objects.sort()
    return objects


def _list_keys_sync(meeting_id: int, session_uid: Optional[str]) -> List[str]:
    return [k for k, _ in _list_objects_sync(meeting_id, session_uid)]


def _dominant_session(objects: List[Tuple[str, int]]) -> Optional[str]:
    """The session_uid holding the most audio bytes.

    Session uids are random UUIDs, so "lexically first" (the previous rule) picked
    an arbitrary session whenever a bot reconnected mid-meeting — sometimes the
    3-second stub instead of the real 90-minute recording. Byte count is the only
    ordering signal available from the key layout, and the real session is the one
    with the audio in it.
    """
    by_session: dict[str, int] = {}
    for key, size in objects:
        by_session[key.split("/")[2]] = by_session.get(key.split("/")[2], 0) + size
    if not by_session:
        return None
    return max(by_session, key=by_session.get)


def _assemble_sync(meeting_id: int, session_uid: Optional[str]) -> Tuple[bytes, str, int]:
    s3 = _client()
    bucket = _bucket()
    objects = _list_objects_sync(meeting_id, session_uid)
    if not objects:
        return b"", "", 0
    # Never splice two sessions' audio together: each MediaRecorder session has
    # its own container header, so a byte-wise concat across sessions is not
    # decodable. Pin to the session that actually holds the recording.
    if session_uid is None:
        chosen = _dominant_session(objects)
        dropped = sum(1 for k, _ in objects if k.split("/")[2] != chosen)
        objects = [(k, s) for k, s in objects if k.split("/")[2] == chosen]
        if dropped:
            logger.warning(
                f"recording_store: meeting {meeting_id} has multiple sessions; assembling "
                f"session {chosen} ({len(objects)} chunks) and skipping {dropped} chunk(s) "
                f"from other session(s)"
            )
    fmt = objects[-1][0].rsplit(".", 1)[-1] or "webm"
    # Accumulate into one pre-sized buffer instead of a list of per-chunk bytes:
    # drops the intermediate list (hundreds of objects, each its own allocation)
    # and avoids realloc-on-grow. The final bytes() copy still transiently doubles
    # the blob, so a long meeting remains the memory-hungry case for meeting-api's
    # 1 GiB cgroup — eliminating that needs the blob spilled to a temp file and
    # streamed to the transcription service, which is a larger change than this.
    buf = bytearray(sum(size for _, size in objects))
    at = 0
    for k, _ in objects:
        body = s3.get_object(Bucket=bucket, Key=k)["Body"].read()
        buf[at:at + len(body)] = body
        at += len(body)
    if at != len(buf):
        # Listing size disagreed with what we actually read (chunk rewritten
        # between list and get). Trust the bytes we have.
        del buf[at:]
    return bytes(buf), fmt, len(objects)


# --- Async API ---------------------------------------------------------------
async def store_chunk(meeting_id: int, session_uid: str, chunk_seq: int, fmt: str, data: bytes) -> str:
    """Persist one audio chunk; returns its S3 key."""
    return await asyncio.to_thread(_put_chunk_sync, meeting_id, session_uid, chunk_seq, fmt, data)


async def list_chunk_keys(meeting_id: int, session_uid: Optional[str] = None) -> List[str]:
    return await asyncio.to_thread(_list_keys_sync, meeting_id, session_uid)


async def assemble_meeting_audio(
    meeting_id: int, session_uid: Optional[str] = None
) -> Tuple[bytes, str, int]:
    """Concatenate all chunks for a meeting into one audio blob.

    MediaRecorder emits a single continuous stream sliced into timeslices where
    only the first slice carries the container header and the rest are
    body/cluster continuations, so a byte-wise concat yields one decodable file
    (Deepgram + ffmpeg both accept it). Returns (bytes, format, chunk_count);
    empty bytes when nothing was recorded.
    """
    return await asyncio.to_thread(_assemble_sync, meeting_id, session_uid)


def _delete_prefix_sync(prefix: str, key_filter=None) -> int:
    s3 = _client()
    bucket = _bucket()
    deleted = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = page.get("Contents", []) or []
        delete_keys = [
            {"Key": obj["Key"]}
            for obj in objects
            if key_filter is None or key_filter(obj["Key"])
        ]
        if not delete_keys:
            continue
        s3.delete_objects(Bucket=bucket, Delete={"Objects": delete_keys})
        deleted += len(delete_keys)
    return deleted


def _delete_meeting_audio_sync(meeting_id: int) -> int:
    return _delete_prefix_sync(
        _prefix(meeting_id),
        key_filter=lambda k: _is_chunk_key(k, meeting_id),
    )


def _delete_meeting_screenshots_sync(meeting_id: int) -> int:
    return _delete_prefix_sync(screenshots_prefix(meeting_id))


async def delete_meeting_audio(meeting_id: int) -> int:
    """Delete every stored chunk for a meeting (all sessions). Returns count deleted."""
    return await asyncio.to_thread(_delete_meeting_audio_sync, meeting_id)


async def delete_meeting_screenshots(meeting_id: int) -> int:
    """Delete the bot's failure-diagnostic screenshots for a meeting.

    These are full-page PNGs uploaded once per failed join; nothing else ever
    removed them, so the prefix grew without bound. Returns count deleted.
    """
    return await asyncio.to_thread(_delete_meeting_screenshots_sync, meeting_id)
