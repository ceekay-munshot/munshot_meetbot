-- Cloudflare D1 schema for the YouTube-transcript mirror.
--
-- Apply once, e.g.:
--   wrangler d1 execute <DB_NAME> --remote --file deploy/cloudflare-d1/schema_youtube_transcripts.sql
-- or paste into the D1 console.
--
-- Two tables mirroring the Postgres split: one metadata row per video plus its
-- timed segments. The PRIMARY KEYs match the upserts emitted by
-- services/meeting-api/meeting_api/collector/d1_youtube_forwarder.py
-- (ON CONFLICT (transcript_id) and ON CONFLICT (transcript_id, segment_index)).
--
-- AWS Postgres remains the source of truth; this is a best-effort read mirror.
-- owner_email is carried on BOTH tables so the Worker can filter a client's
-- transcripts without joining, exactly like the `transcriptions` mirror.
--
-- Deliberately NOT stored here: the concatenated transcript text. A long video's
-- text would be a single multi-hundred-KB bound parameter; the frontend assembles
-- it from segment rows the same way it already does for meeting transcripts.

CREATE TABLE IF NOT EXISTS youtube_transcripts (
  transcript_id     INTEGER PRIMARY KEY,
  user_id           INTEGER NOT NULL,
  owner_email       TEXT,
  video_id          TEXT    NOT NULL,
  url               TEXT    NOT NULL,
  title             TEXT,
  channel           TEXT,
  duration_seconds  INTEGER,
  -- queued | processing | completed | failed
  status            TEXT    NOT NULL,
  -- captions (YouTube's own subtitles) | deepgram (ASR on the audio)
  source            TEXT,
  language          TEXT,
  segment_count     INTEGER,
  error             TEXT,
  created_at        TEXT,
  updated_at        TEXT,
  completed_at      TEXT
);

CREATE INDEX IF NOT EXISTS ix_youtube_transcripts_owner_created
  ON youtube_transcripts (owner_email, created_at);
CREATE INDEX IF NOT EXISTS ix_youtube_transcripts_user_created
  ON youtube_transcripts (user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_youtube_transcripts_video
  ON youtube_transcripts (video_id);
CREATE INDEX IF NOT EXISTS ix_youtube_transcripts_status
  ON youtube_transcripts (status);

CREATE TABLE IF NOT EXISTS youtube_transcript_segments (
  transcript_id   INTEGER NOT NULL,
  segment_index   INTEGER NOT NULL,
  start_time      REAL,
  end_time        REAL,
  text            TEXT,
  -- NULL on the captions path (YouTube gives no diarization); a Deepgram
  -- speaker label on the ASR path.
  speaker         TEXT,
  language        TEXT,
  owner_email     TEXT,
  PRIMARY KEY (transcript_id, segment_index)
);

CREATE INDEX IF NOT EXISTS ix_youtube_segments_owner
  ON youtube_transcript_segments (owner_email);
CREATE INDEX IF NOT EXISTS ix_youtube_segments_transcript_start
  ON youtube_transcript_segments (transcript_id, start_time);
