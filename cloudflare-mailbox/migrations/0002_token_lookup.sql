ALTER TABLE aliases ADD COLUMN token_lookup_digest TEXT;

CREATE UNIQUE INDEX aliases_token_lookup_digest_idx
    ON aliases (token_lookup_digest)
    WHERE token_lookup_digest IS NOT NULL;
