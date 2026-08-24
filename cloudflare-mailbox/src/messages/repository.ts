export interface MessageRow {
  id: string;
  alias_digest: string;
  message_digest: string;
  payload_ciphertext: string;
  payload_iv: string;
  received_at: number;
  expires_at: number;
  created_at: number;
  category: "gpt" | "grok" | "other";
  has_code: number;
  retention_class: "temporary" | "permanent";
}

export interface AdminMessageRow extends MessageRow {
  alias_secret_ciphertext: string;
  alias_secret_iv: string;
}

export interface MessageWrite {
  id: string;
  aliasDigest: string;
  messageDigest: string;
  payloadCiphertext: string;
  payloadIv: string;
  receivedAt: number;
  expiresAt: number;
  createdAt: number;
  category: "gpt" | "grok" | "other";
  hasCode: boolean;
  retentionClass: "temporary" | "permanent";
}

export class MessageRepository {
  constructor(private readonly database: D1Database) {}

  async insert(value: MessageWrite): Promise<boolean> {
    const result = await this.database
      .prepare(
        `INSERT OR IGNORE INTO messages (
            id, alias_digest, message_digest, payload_ciphertext,
            payload_iv, received_at, expires_at, created_at, category, has_code,
            retention_class
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .bind(
        value.id,
        value.aliasDigest,
        value.messageDigest,
        value.payloadCiphertext,
        value.payloadIv,
        value.receivedAt,
        value.expiresAt,
        value.createdAt,
        value.category,
        value.hasCode ? 1 : 0,
        value.retentionClass,
      )
      .run();
    return Number(result.meta.changes ?? 0) > 0;
  }

  async listPublicCodes(
    aliasDigest: string,
    now: number,
    limit: number,
  ): Promise<MessageRow[]> {
    const result = await this.database
      .prepare(
        `SELECT id, alias_digest, message_digest, payload_ciphertext,
                payload_iv, received_at, expires_at, created_at, category, has_code,
                retention_class
           FROM messages
          WHERE alias_digest = ? AND expires_at > ?
            AND category IN ('gpt', 'grok') AND has_code = 1
          ORDER BY received_at DESC, created_at DESC
          LIMIT ?`,
      )
      .bind(aliasDigest, now, limit)
      .all<MessageRow>();
    return result.results;
  }

  async listAll(now: number, limit: number): Promise<AdminMessageRow[]> {
    const result = await this.database
      .prepare(
        `SELECT m.id, m.alias_digest, m.message_digest, m.payload_ciphertext,
                m.payload_iv, m.received_at, m.expires_at, m.created_at,
                m.category, m.has_code,
                m.retention_class,
                a.secret_ciphertext AS alias_secret_ciphertext,
                a.secret_iv AS alias_secret_iv
           FROM messages AS m
           JOIN aliases AS a ON a.alias_digest = m.alias_digest
          WHERE m.expires_at > ?
          ORDER BY m.received_at DESC, m.created_at DESC
          LIMIT ?`,
      )
      .bind(now, limit)
      .all<AdminMessageRow>();
    return result.results;
  }

  async cleanup(now: number): Promise<number> {
    const result = await this.database
      .prepare(
        "DELETE FROM messages WHERE retention_class = 'temporary' AND expires_at <= ?",
      )
      .bind(now)
      .run();
    return Number(result.meta.changes ?? 0);
  }
}
