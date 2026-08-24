ALTER TABLE messages
    ADD COLUMN category TEXT NOT NULL DEFAULT 'other'
    CHECK (category IN ('gpt', 'grok', 'other'));

ALTER TABLE messages
    ADD COLUMN has_code INTEGER NOT NULL DEFAULT 0
    CHECK (has_code IN (0, 1));

ALTER TABLE messages
    ADD COLUMN retention_class TEXT NOT NULL DEFAULT 'permanent'
    CHECK (retention_class IN ('temporary', 'permanent'));

CREATE INDEX messages_public_codes_idx
    ON messages (alias_digest, category, has_code, received_at DESC);

CREATE INDEX messages_retention_idx
    ON messages (retention_class, expires_at);
