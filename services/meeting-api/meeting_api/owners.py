"""Multi-owner materialization for meetings.

A meeting can have many owners (co-viewers). Owners come from the calendar
event's attendee list: every attendee email — plus the organizer / the user
who scheduled the bot — becomes an owner so each of them can later see the
meeting and its transcript they were invited to.

Key rules:
  * Users are auto-created for attendee emails that don't exist yet.
  * ``meeting_owners`` is the many-to-many link (see models.MeetingOwner).
  * Everything here is best-effort at the call site (calendar scheduling) —
    a failure to materialize owners must NEVER block bot dispatch.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from admin_models.models import User
from .models import MeetingOwner

logger = logging.getLogger(__name__)


def _norm_email(email: Any) -> Optional[str]:
    if not email or not isinstance(email, str):
        return None
    email = email.strip().lower()
    return email if "@" in email else None


def normalize_attendees(attendees: Any) -> list[dict]:
    """Reduce a raw Google Calendar attendee list to owner candidates.

    Drops meeting-room resources and entries without a usable email, and
    de-duplicates by normalized email. Returns dicts with ``email``,
    ``name``, ``role`` ('organizer'|'attendee').
    """
    out: dict[str, dict] = {}
    if not isinstance(attendees, Iterable) or isinstance(attendees, (str, bytes)):
        return []
    for a in attendees:
        if not isinstance(a, dict):
            continue
        if a.get("resource"):  # a room / equipment, not a person
            continue
        email = _norm_email(a.get("email"))
        if not email:
            continue
        role = "organizer" if a.get("organizer") else "attendee"
        # An earlier organizer entry for the same email wins over attendee.
        if email in out and out[email]["role"] == "organizer":
            continue
        out[email] = {
            "email": email,
            "name": a.get("displayName") or None,
            "role": role,
        }
    return list(out.values())


async def find_or_create_user_by_email(
    db: AsyncSession, email: str, name: Optional[str] = None
) -> Optional[User]:
    """Return the users row for ``email``, creating it if absent.

    Race-safe: relies on the ``users.email`` unique constraint via an
    INSERT ... ON CONFLICT DO NOTHING followed by a SELECT. Returns None only
    if ``email`` is unusable.
    """
    email = _norm_email(email)
    if not email:
        return None

    existing = (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if existing:
        # Backfill a name if we learned one and the row had none.
        if name and not existing.name:
            existing.name = name
        return existing

    stmt = (
        pg_insert(User)
        .values(email=email, name=name)
        .on_conflict_do_nothing(index_elements=[User.email])
    )
    await db.execute(stmt)
    return (
        await db.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()


async def upsert_meeting_owners(
    db: AsyncSession,
    meeting_id: int,
    attendees: Any,
    primary_user_id: Optional[int] = None,
    primary_email: Optional[str] = None,
) -> list[MeetingOwner]:
    """Materialize all owners of ``meeting_id`` from a calendar attendee list.

    * Auto-creates a user for every attendee email.
    * Always includes the scheduling user (``primary_user_id`` / ``primary_email``)
      as an owner even if they aren't in the attendee list.
    * Idempotent: re-running upserts the same rows (unique on meeting_id+user_id).

    Does not commit — the caller owns the transaction. Returns the MeetingOwner
    rows that now exist for this meeting's freshly-processed candidates.
    """
    candidates = normalize_attendees(attendees)

    # Ensure the scheduler themself is an owner (organizer role, requester source).
    primary_email = _norm_email(primary_email)
    if primary_email and not any(c["email"] == primary_email for c in candidates):
        candidates.append({"email": primary_email, "name": None, "role": "organizer"})

    created: list[MeetingOwner] = []
    for c in candidates:
        user = await find_or_create_user_by_email(db, c["email"], c.get("name"))
        if not user:
            continue
        source = "requester" if user.id == primary_user_id else "calendar"
        stmt = (
            pg_insert(MeetingOwner)
            .values(
                meeting_id=meeting_id,
                user_id=user.id,
                email=c["email"],
                role=c["role"],
                source=source,
            )
            .on_conflict_do_nothing(
                constraint="uq_meeting_owner_meeting_user"
            )
        )
        await db.execute(stmt)
        created.append(
            MeetingOwner(
                meeting_id=meeting_id,
                user_id=user.id,
                email=c["email"],
                role=c["role"],
                source=source,
            )
        )
    logger.info(
        "meeting %s: materialized %d owner(s) from %d attendee candidate(s)",
        meeting_id, len(created), len(candidates),
    )
    return created


async def resolve_meeting_owner_emails(
    db: AsyncSession, meeting_id: int
) -> list[str]:
    """All owner emails for a meeting (for the D1 mirror). Best-effort: [] on error."""
    try:
        rows = (
            await db.execute(
                select(MeetingOwner.email).where(MeetingOwner.meeting_id == meeting_id)
            )
        ).scalars().all()
        return [e for e in rows if e]
    except Exception as e:  # noqa: BLE001
        logger.error("Could not resolve owner emails for meeting %s: %s", meeting_id, e)
        return []
