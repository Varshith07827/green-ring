-- Queue of messages waiting to go out to WhatsApp.
CREATE TABLE IF NOT EXISTS queue (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id   TEXT NOT NULL UNIQUE,   -- handed to the bridge as _id, for dedup
  dest         TEXT NOT NULL,          -- phone with country code, or a group jid
  text         TEXT NOT NULL,
  created_at   INTEGER NOT NULL,
  delivered_at INTEGER                 -- NULL until a poll claims the row
);

-- The poll reads exactly this: oldest undelivered first.
CREATE INDEX IF NOT EXISTS queue_pending ON queue (delivered_at, id);
