export interface MessageRow {
  id: string;
  alias_digest: string;
  message_digest: string;
  payload_ciphertext: string;
  payload_iv: string;
  received_at: number;
  expires_at: number;
  created_at: number;
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
}

export class MessageRepository {
  constructor(private readonly database: D1Database) {}

  async insert(value: MessageWrite): Promise<boolean> {
    const result = await this.database
      .prepare(
        `INSERT OR IGNORE INTO messages (
            id, alias_digest, message_digest, payload_ciphertext,
            payload_iv, received_at, expires_at, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
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
      )
      .run();
    return Number(result.meta.changes ?? 0) > 0;
  }

  async list(
    aliasDigest: string,
    now: number,
    limit: number,
  ): Promise<MessageRow[]> {
    const result = await this.database
      .prepare(
        `SELECT id, alias_digest, message_digest, payload_ciphertext,
                payload_iv, received_at, expires_at, created_at
           FROM messages
          WHERE alias_digest = ? AND expires_at > ?
          ORDER BY received_at DESC, created_at DESC
          LIMIT ?`,
      )
      .bind(aliasDigest, now, limit)
      .all<MessageRow>();
    return result.results;
  }

  async cleanup(now: number): Promise<number> {
    const result = await this.database
      .prepare("DELETE FROM messages WHERE expires_at <= ?")
      .bind(now)
      .run();
    return Number(result.meta.changes ?? 0);
  }
}
