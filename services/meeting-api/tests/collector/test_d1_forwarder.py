"""Unit tests for the Cloudflare D1 transcript mirror — owner_email wiring.

These exercise _build_chunks (pure, no network) to assert that each row carries
the owning user's email so the Cloudflare frontend can filter per-client.
"""
from types import SimpleNamespace

from meeting_api.collector import d1_forwarder
from meeting_api.collector.d1_forwarder import _COLUMNS, _build_chunks


def _seg(meeting_id, segment_id, text="hi"):
    return SimpleNamespace(
        meeting_id=meeting_id,
        segment_id=segment_id,
        start_time=1.0,
        end_time=2.0,
        text=text,
        speaker="S1",
        language="en",
        session_uid="uid-1",
        created_at=None,
    )


def test_owner_email_is_a_column():
    assert "owner_email" in _COLUMNS
    # owner_email must be the LAST param so _row_params order matches the INSERT.
    assert _COLUMNS[-1] == "owner_email"


def test_build_chunks_appends_owner_email_per_meeting():
    segs = [_seg(10, "a"), _seg(20, "b")]
    owner_emails = {10: "alice@acme.com", 20: "bob@acme.com"}

    chunks = _build_chunks(segs, owner_emails)

    assert len(chunks) == 1
    params = chunks[0]["params"]
    ncols = len(_COLUMNS)
    assert len(params) == ncols * 2

    # Each row's last param is its meeting's owner email.
    row0 = params[0:ncols]
    row1 = params[ncols : 2 * ncols]
    assert row0[-1] == "alice@acme.com"
    assert row1[-1] == "bob@acme.com"
    # SQL upsert keeps owner_email fresh on conflict.
    assert "owner_email=excluded.owner_email" in chunks[0]["sql"]
    assert "owner_email" in chunks[0]["sql"].split("VALUES")[0]


def test_missing_owner_email_writes_null():
    segs = [_seg(10, "a")]
    # No entry for meeting 10 -> NULL owner_email, mirror still proceeds.
    chunks = _build_chunks(segs, {})

    params = chunks[0]["params"]
    assert params[-1] is None


def test_segments_without_segment_id_are_skipped():
    segs = [_seg(10, None), _seg(10, "a")]
    chunks = _build_chunks(segs, {10: "alice@acme.com"})

    # Only the segment_id-bearing row survives (D1 PK needs it).
    assert len(chunks) == 1
    assert len(chunks[0]["params"]) == len(_COLUMNS)
