-- Match the operator inbox ordering so LIMIT can stop after the visible page,
-- rather than reading and sorting the full retained archive on every poll.
CREATE INDEX IF NOT EXISTS messages_admin_received_idx
    ON messages (received_at DESC, created_at DESC, id DESC);
