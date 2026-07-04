"""Mirror meeting ownership (many-to-many) into a Cloudflare D1 table.

Companion to ``d1_meeting_forwarder.py`` (meeting state) and
``d1_forwarder.py`` (transcript segments). A meeting can have multiple owners
— every calendar attendee — and the frontend needs to list a meeting for each
of them. The transcript rows carry only a single ``owner_email`` (the primary
owner), so this table is what lets the Cloudflare side answer "which meetings
can email X see".

Shape: rows of ``(meeting_id, owner_email)``. The frontend lists a client's
meetings via ``SELECT meeting_id FROM meeting_owners WHERE owner_email = ?``.

Key rules (same contract as the other D1 sinks):
  * Postgres remains the source of truth; D1 is a best-effort mirror.
  * Never raises into the caller — a D1 outage must not break scheduling.
  * Idempotent upsert keyed on ``(meeting_id, owner_email)``.

Enable via ``CLOUDFLARE_D1_ENABLED=true`` + CF_* creds (see config.py).
Schema: deploy/cloudflare-d1/schema_meeting_owners.sql.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

import httpx

from .config import (
    CLOUDFLARE_D1_ENABLED,
    CF_ACCOUNT_ID,
    CF_D1_DATABASE_ID,
    CF_API_TOKEN,
    CF_D1_OWNERS_TABLE,
    CF_D1_TIMEOUT_SECONDS,
)

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
            "Cloudflare D1 owners mirror enabled but missing config: %s. Skipping.",
            ", ".join(missing),
        )
        return False
    return True


# D1 caps a single statement at 100 BOUND PARAMETERS. 2 cols/row → 50 rows max;
# keep a margin. A single meeting rarely has more than a handful of owners.
_ROWS_PER_CHUNK = 40


async def _send_chunk(client: httpx.AsyncClient, url: str, headers: dict, chunk: dict) -> bool:
    resp = await client.post(url, headers=headers, json=chunk)
    if resp.status_code != 200:
        logger.error("D1 owners forward HTTP %s: %s", resp.status_code, resp.text[:500])
        return False
    body = resp.json()
    if not body.get("success", False):
        logger.error("D1 owners forward returned errors: %s", body.get("errors"))
        return False
    return True


async def mirror_meeting_owners_to_d1(
    meeting_id: int, owner_emails: Iterable[str]
) -> None:
    """Best-effort mirror of a meeting's full owner list to Cloudflare D1.

    Upserts one ``(meeting_id, owner_email)`` row per owner. Does NOT delete
    owners removed elsewhere (ownership only grows here); the unique PK makes
    re-sends idempotent. Catches all errors; never raises.
    """
    if not _is_configured():
        return
    if meeting_id is None:
        return

    emails = sorted({
        e.strip().lower()
        for e in (owner_emails or [])
        if e and isinstance(e, str) and "@" in e
    })
    if not emails:
        return

    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json",
    }
    url = _d1_query_url()

    chunks = []
    for i in range(0, len(emails), _ROWS_PER_CHUNK):
        batch = emails[i : i + _ROWS_PER_CHUNK]
        values = ", ".join(["(?, ?)"] * len(batch))
        sql = (
            f"INSERT INTO {CF_D1_OWNERS_TABLE} (meeting_id, owner_email) "
            f"VALUES {values} ON CONFLICT (meeting_id, owner_email) DO NOTHING"
        )
        params: list[str] = []
        for e in batch:
            params.extend([str(meeting_id), e])
        chunks.append({"sql": sql, "params": params})

    try:
        sent = 0
        async with httpx.AsyncClient(timeout=CF_D1_TIMEOUT_SECONDS) as client:
            for chunk in chunks:
                if await _send_chunk(client, url, headers, chunk):
                    sent += len(chunk["params"]) // 2
        if sent:
            logger.info("Mirrored %d owner(s) for meeting %s to Cloudflare D1", sent, meeting_id)
    except httpx.RequestError as e:
        logger.error("D1 owners forward request error (non-fatal): %s", e)
    except Exception as e:  # noqa: BLE001 - sink must never propagate
        logger.error("D1 owners forward unexpected error (non-fatal): %s", e, exc_info=True)
