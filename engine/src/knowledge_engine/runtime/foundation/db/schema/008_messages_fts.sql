-- 008 board full-text search (FTS5)
-- ke:optional
--
-- Consumers: agent_board.store.search_messages (bm25 ranking + snippets).
-- Optional: on a SQLite build without FTS5 this migration is skipped (and
-- retried on the next open) and board search falls back to a LIKE scan.
--
-- Standard (non-contentless) FTS5: it stores its own copy of subject + body
-- so snippet() and bm25() work without joining back to messages. Bodies are
-- bounded to ~50KB by the post validator, so the extra storage is small.
-- Messages written before this table existed are mirrored into it by
-- runtime.foundation.db (one-shot backfill per database per process).

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    message_id UNINDEXED,
    channel,
    subject,
    body
);

CREATE TRIGGER IF NOT EXISTS messages_fts_ai
AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(message_id, channel, subject, body)
    VALUES (new.message_id, new.channel, COALESCE(new.subject,''), COALESCE(new.body,''));
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_ad
AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE message_id = old.message_id;
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_au
AFTER UPDATE OF subject, body, channel ON messages BEGIN
    DELETE FROM messages_fts WHERE message_id = old.message_id;
    INSERT INTO messages_fts(message_id, channel, subject, body)
    VALUES (new.message_id, new.channel, COALESCE(new.subject,''), COALESCE(new.body,''));
END;
