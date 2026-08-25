CREATE TABLE message_attachments (
    -- Deliberately no foreign key: attachment object keys must survive long
    -- enough for the scheduled orphan reconciler to delete KV bytes after an
    -- Alias or message is removed by another control-plane transaction.
    id TEXT PRIMARY KEY,
    message_id TEXT NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    metadata_ciphertext TEXT NOT NULL,
    metadata_iv TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    created_at INTEGER NOT NULL
) STRICT;

CREATE INDEX message_attachments_message_idx
    ON message_attachments (message_id, created_at, id);
