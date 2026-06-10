-- Cloudflare D1 schema for the Vexa transcript mirror.
-- Mirrors services/meeting-api/meeting_api/models.py :: Transcription.
--
-- Apply once, e.g.:
--   wrangler d1 execute <DB_NAME> --remote --file deploy/cloudflare-d1/schema.sql
-- or paste into the D1 console in the Cloudflare dashboard.
--
-- The composite PRIMARY KEY (meeting_id, segment_id) makes the forwarder's
-- INSERT ... ON CONFLICT DO UPDATE idempotent, matching the Postgres unique
-- index on (meeting_id, segment_id). Re-sent segments overwrite, never duplicate.

CREATE TABLE IF NOT EXISTS transcriptions (
  meeting_id   INTEGER NOT NULL,
  segment_id   TEXT    NOT NULL,
  start_time   REAL    NOT NULL,
  end_time     REAL    NOT NULL,
  text         TEXT    NOT NULL,
  speaker      TEXT,
  language     TEXT,
  session_uid  TEXT,
  created_at   TEXT,
  PRIMARY KEY (meeting_id, segment_id)
);

CREATE INDEX IF NOT EXISTS ix_transcriptions_meeting_start
  ON transcriptions (meeting_id, start_time);
CREATE INDEX IF NOT EXISTS ix_transcriptions_session
  ON transcriptions (session_uid);
