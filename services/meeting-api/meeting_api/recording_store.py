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


def _list_keys_sync(meeting_id: int, session_uid: Optional[str]) -> List[str]:
    s3 = _client()
    bucket = _bucket()
    keys: List[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=_prefix(meeting_id, session_uid)):
        for obj in page.get("Contents", []) or []:
            keys.append(obj["Key"])
    # Lexical sort == numeric because seq is zero-padded; if multiple sessions
    # are present (session_uid=None), they group by session then by seq.
    keys.sort()
    return keys


def _assemble_sync(meeting_id: int, session_uid: Optional[str]) -> Tuple[bytes, str, int]:
    s3 = _client()
    bucket = _bucket()
    keys = _list_keys_sync(meeting_id, session_uid)
    if not keys:
        return b"", "", 0
    # If no session pinned, use the session of the FIRST (lexically) chunk so we
    # never splice two different sessions' audio together.
    if session_uid is None:
        first_session = keys[0].split("/")[2]
        keys = [k for k in keys if k.split("/")[2] == first_session]
    fmt = keys[-1].rsplit(".", 1)[-1] or "webm"
    parts: List[bytes] = []
    for k in keys:
        body = s3.get_object(Bucket=bucket, Key=k)["Body"].read()
        parts.append(body)
    return b"".join(parts), fmt, len(keys)


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
