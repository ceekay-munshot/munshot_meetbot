"""Mirror finalized transcript segments into a Cloudflare D1 database.

This is a best-effort sink invoked by ``process_redis_to_postgres`` right after
finalized segments are committed to Postgres. It must NEVER raise into the caller:
a D1 outage should leave Vexa's own Postgres authoritative and uninterrupted.

Enable by setting CLOUDFLARE_D1_ENABLED=true plus the CF_* credentials (see config.py).
"""

import logging
from typing import List

import httpx

from .config import (
    CLOUDFLARE_D1_ENABLED,
    CF_ACCOUNT_ID,
    CF_D1_DATABASE_ID,
    CF_API_TOKEN,
    CF_D1_TABLE,
    CF_D1_TIMEOUT_SECONDS,
)
from ..models import Transcription

logger = logging.getLogger(__name__)


def _d1_query_url() -> str:
    return (
        f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}"
        f"/d1/database/{CF_D1_DATABASE_ID}/query"
    )


def _is_configured() -> bool:
    if not CLOUDFLARE_D1_ENABLED:
        return False
    missing = [
        name
        for name, val in (
            ("CF_ACCOUNT_ID", CF_ACCOUNT_ID),
            ("CF_D1_DATABASE_ID", CF_D1_DATABASE_ID),
            ("CF_API_TOKEN", CF_API_TOKEN),
        )
        if not val
    ]
    if missing:
        logger.warning(
            "Cloudflare D1 forwarding enabled but missing config: %s. Skipping.",
            ", ".join(missing),
        )
        return False
    return True


# 9 columns per row; SQLite caps bound variables at 999, so keep chunks well under
# that (50 rows = 450 params). Batches here are normally far smaller than one chunk.
_COLUMNS = (
    "meeting_id",
    "segment_id",
    "start_time",
    "end_time",
    "text",
    "speaker",
    "language",
    "session_uid",
    "created_at",
)
_ROWS_PER_CHUNK = 50


def _p(value):
    """D1's /query params are documented as strings; SQLite column affinity coerces
    numeric strings back to INTEGER/REAL on insert. Keep NULLs as JSON null."""
    return None if value is None else str(value)


def _row_params(s: Transcription) -> list:
    return [
        _p(s.meeting_id),
        _p(s.segment_id),
        _p(s.start_time),
        _p(s.end_time),
        _p(s.text),
        _p(s.speaker),
        _p(s.language),
        _p(s.session_uid),
        _p(s.created_at.isoformat() if s.created_at else None),
    ]


def _build_chunks(segments: List[Transcription]):
    """Build a list of ``{sql, params}`` single-statement upserts.

    Each chunk is one multi-row ``INSERT ... ON CONFLICT DO UPDATE`` with all row
    params flattened into a single ``params`` array — the documented D1 single-query
    format. Segments without a segment_id can't be deduplicated by D1's composite
    primary key, so they are skipped (rare legacy rows; Postgres keeps them anyway).
    """
    eligible = [s for s in segments if s.segment_id]
    skipped = len(segments) - len(eligible)
    if skipped:
        logger.debug("D1 forward skipping %d segment(s) lacking segment_id", skipped)

    placeholder = "(" + ", ".join(["?"] * len(_COLUMNS)) + ")"
    cols = ", ".join(_COLUMNS)
    upsert = (
        "ON CONFLICT (meeting_id, segment_id) DO UPDATE SET "
        "text=excluded.text, speaker=excluded.speaker, end_time=excluded.end_time, "
        "language=excluded.language, created_at=excluded.created_at"
    )

    chunks = []
    for i in range(0, len(eligible), _ROWS_PER_CHUNK):
        rows = eligible[i : i + _ROWS_PER_CHUNK]
        values = ", ".join([placeholder] * len(rows))
        sql = f"INSERT INTO {CF_D1_TABLE} ({cols}) VALUES {values} {upsert}"
        params = [p for s in rows for p in _row_params(s)]
        chunks.append({"sql": sql, "params": params})
    return chunks


async def _send_chunk(client: httpx.AsyncClient, url: str, headers: dict, chunk: dict) -> bool:
    resp = await client.post(url, headers=headers, json=chunk)
    if resp.status_code != 200:
        logger.error("D1 forward HTTP %s: %s", resp.status_code, resp.text[:500])
        return False
    body = resp.json()
    if not body.get("success", False):
        logger.error("D1 forward returned errors: %s", body.get("errors"))
        return False
    return True


async def forward_segments_to_d1(segments: List[Transcription]) -> None:
    """Best-effort mirror of finalized segments to Cloudflare D1.

    Catches all errors internally and logs them; never raises.
    """
    if not segments or not _is_configured():
        return

    chunks = _build_chunks(segments)
    if not chunks:
        return

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }
    url = _d1_query_url()

    try:
        sent = 0
        async with httpx.AsyncClient(timeout=CF_D1_TIMEOUT_SECONDS) as client:
            for chunk in chunks:
                if await _send_chunk(client, url, headers, chunk):
                    # params is the flattened row list; rows = len(params) / columns
                    sent += len(chunk["params"]) // len(_COLUMNS)
        if sent:
            logger.info("Mirrored %d segment(s) to Cloudflare D1", sent)
    except httpx.RequestError as e:
        logger.error("D1 forward request error (non-fatal): %s", e)
    except Exception as e:  # noqa: BLE001 - sink must never propagate
        logger.error("D1 forward unexpected error (non-fatal): %s", e, exc_info=True)
