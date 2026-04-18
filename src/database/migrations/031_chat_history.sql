CREATE TABLE IF NOT EXISTS chat_history (
  id               BIGSERIAL PRIMARY KEY,
  participant_id   TEXT NOT NULL,
  participant_name TEXT NOT NULL,
  participant_type TEXT NOT NULL CHECK (participant_type IN ('user','agent')),
  channel_id       TEXT,
  role             TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content          TEXT NOT NULL,
  created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS chat_history_participant_time
  ON chat_history(participant_id, created_at DESC);
