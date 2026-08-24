PRAGMA foreign_keys = ON;

CREATE TABLE aliases (
    alias_digest TEXT PRIMARY KEY,
    external_id TEXT NOT NULL DEFAULT '',
    secret_ciphertext TEXT NOT NULL,
    secret_iv TEXT NOT NULL,
    token_digest TEXT,
    state TEXT NOT NULL CHECK (state IN ('active', 'inactive')),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;

CREATE INDEX aliases_state_idx ON aliases (state);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    alias_digest TEXT NOT NULL REFERENCES aliases(alias_digest) ON DELETE CASCADE,
    message_digest TEXT NOT NULL,
    payload_ciphertext TEXT NOT NULL,
    payload_iv TEXT NOT NULL,
    received_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
) STRICT;

CREATE UNIQUE INDEX messages_alias_digest_unique_idx
    ON messages (alias_digest, message_digest);
CREATE INDEX messages_alias_received_idx
    ON messages (alias_digest, received_at DESC);
CREATE INDEX messages_expiry_idx ON messages (expires_at);

CREATE TABLE auth_rate_limits (
    fingerprint TEXT NOT NULL,
    window_started INTEGER NOT NULL,
    attempts INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (fingerprint, window_started)
) STRICT;

CREATE INDEX auth_rate_limits_updated_idx ON auth_rate_limits (updated_at);
