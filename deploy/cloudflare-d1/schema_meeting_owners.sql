-- Cloudflare D1 schema for meeting OWNERSHIP (many-to-many).
-- Companion to schema.sql (transcript segments) and schema_meetings.sql
-- (meeting state). A meeting can have multiple owners — every calendar
-- attendee — and each of them must be able to see it.
--
-- Apply once, e.g.:
--   wrangler d1 execute <DB_NAME> --remote --file deploy/cloudflare-d1/schema_meeting_owners.sql
-- or paste into the D1 console.
--
-- The composite PRIMARY KEY (meeting_id, owner_email) matches the upsert
-- (INSERT ... ON CONFLICT (meeting_id, owner_email) DO NOTHING) emitted by
-- services/meeting-api/meeting_api/collector/d1_owners_forwarder.py.
-- AWS Postgres (meeting_owners table) remains the source of truth.
--
-- FRONTEND USAGE — list every meeting a client (by email) can see:
--   SELECT m.*
--   FROM meetings m
--   JOIN meeting_owners o ON o.meeting_id = m.meeting_id
--   WHERE o.owner_email = ?1;
-- and read that meeting's transcript by meeting_id (no owner_email filter
-- needed once ownership is established here). The single transcriptions.owner_email
-- column still works for the primary owner and is kept for back-compat.

CREATE TABLE IF NOT EXISTS meeting_owners (
  meeting_id   INTEGER NOT NULL,
  owner_email  TEXT    NOT NULL,
  PRIMARY KEY (meeting_id, owner_email)
);

-- Primary access path: "what meetings can this email see".
CREATE INDEX IF NOT EXISTS ix_meeting_owners_email
  ON meeting_owners (owner_email);
