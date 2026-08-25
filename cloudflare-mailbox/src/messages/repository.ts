import { AppError } from "../shared/errors";

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

export interface AdminMessageCursor {
  receivedAt: number;
  createdAt: number;
  id: string;
}

export interface AdminMessageRowsPage {
  rows: AdminMessageRow[];
  hasMore: boolean;
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

export interface MessageInsertResult {
  id: string;
  inserted: boolean;
}

export interface AttachmentRow {
  id: string;
  message_id: string;
  object_key: string;
  metadata_ciphertext: string;
  metadata_iv: string;
  size_bytes: number;
  created_at: number;
}

export interface AttachmentWrite {
  id: string;
  messageId: string;
  objectKey: string;
  metadataCiphertext: string;
  metadataIv: string;
  sizeBytes: number;
  createdAt: number;
}

export class MessageRepository {
  constructor(private readonly database: D1Database) {}

  async insert(value: MessageWrite): Promise<MessageInsertResult> {
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
    if (Number(result.meta.changes ?? 0) > 0) {
      return { id: value.id, inserted: true };
    }
    const existing = await this.database
      .prepare(
        `SELECT id
           FROM messages
          WHERE alias_digest = ? AND message_digest = ?`,
      )
      .bind(value.aliasDigest, value.messageDigest)
      .first<{ id: string }>();
    if (!existing) {
      throw new AppError("database_error", 500, "邮件保存失败。");
    }
    return { id: existing.id, inserted: false };
  }

  getById(messageId: string, now: number): Promise<MessageRow | null> {
    return this.database
      .prepare(
        `SELECT id, alias_digest, message_digest, payload_ciphertext,
                payload_iv, received_at, expires_at, created_at, category, has_code,
                retention_class
           FROM messages
          WHERE id = ? AND expires_at > ?`,
      )
      .bind(messageId, now)
      .first<MessageRow>();
  }

  async upsertAttachment(value: AttachmentWrite): Promise<void> {
    await this.database
      .prepare(
        `INSERT INTO message_attachments (
            id, message_id, object_key, metadata_ciphertext, metadata_iv,
            size_bytes, created_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET
            message_id = excluded.message_id,
            object_key = excluded.object_key,
            metadata_ciphertext = excluded.metadata_ciphertext,
            metadata_iv = excluded.metadata_iv,
            size_bytes = excluded.size_bytes`,
      )
      .bind(
        value.id,
        value.messageId,
        value.objectKey,
        value.metadataCiphertext,
        value.metadataIv,
        value.sizeBytes,
        value.createdAt,
      )
      .run();
  }

  async listAttachments(messageIds: string[]): Promise<AttachmentRow[]> {
    if (!messageIds.length) return [];
    const placeholders = messageIds.map(() => "?").join(", ");
    const result = await this.database
      .prepare(
        `SELECT id, message_id, object_key, metadata_ciphertext, metadata_iv,
                size_bytes, created_at
           FROM message_attachments
          WHERE message_id IN (${placeholders})
          ORDER BY created_at, id`,
      )
      .bind(...messageIds)
      .all<AttachmentRow>();
    return result.results;
  }

  getAttachment(
    messageId: string,
    attachmentId: string,
    now: number,
  ): Promise<AttachmentRow | null> {
    return this.database
      .prepare(
        `SELECT ma.id, ma.message_id, ma.object_key, ma.metadata_ciphertext,
                ma.metadata_iv, ma.size_bytes, ma.created_at
           FROM message_attachments AS ma
           JOIN messages AS m ON m.id = ma.message_id
          WHERE ma.message_id = ? AND ma.id = ? AND m.expires_at > ?`,
      )
      .bind(messageId, attachmentId, now)
      .first<AttachmentRow>();
  }

  async listExpiredAttachments(now: number): Promise<AttachmentRow[]> {
    const result = await this.database
      .prepare(
        `SELECT ma.id, ma.message_id, ma.object_key, ma.metadata_ciphertext,
                ma.metadata_iv, ma.size_bytes, ma.created_at
           FROM message_attachments AS ma
           JOIN messages AS m ON m.id = ma.message_id
          WHERE m.retention_class = 'temporary' AND m.expires_at <= ?
          LIMIT 100`,
      )
      .bind(now)
      .all<AttachmentRow>();
    return result.results;
  }

  async listOrphanAttachments(): Promise<AttachmentRow[]> {
    const result = await this.database
      .prepare(
        `SELECT ma.id, ma.message_id, ma.object_key, ma.metadata_ciphertext,
                ma.metadata_iv, ma.size_bytes, ma.created_at
           FROM message_attachments AS ma
           LEFT JOIN messages AS m ON m.id = ma.message_id
          WHERE m.id IS NULL
          LIMIT 100`,
      )
      .all<AttachmentRow>();
    return result.results;
  }

  async deleteAttachmentRows(ids: string[]): Promise<void> {
    if (!ids.length) return;
    const placeholders = ids.map(() => "?").join(", ");
    await this.database
      .prepare(`DELETE FROM message_attachments WHERE id IN (${placeholders})`)
      .bind(...ids)
      .run();
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

  async listAll(
    now: number,
    limit: number,
    cursor: AdminMessageCursor | null = null,
  ): Promise<AdminMessageRowsPage> {
    const cursorClause = cursor
      ? `AND (
            m.received_at < ? OR
            (m.received_at = ? AND m.created_at < ?) OR
            (m.received_at = ? AND m.created_at = ? AND m.id < ?)
         )`
      : "";
    const bindings: Array<string | number> = [now];
    if (cursor) {
      bindings.push(
        cursor.receivedAt,
        cursor.receivedAt,
        cursor.createdAt,
        cursor.receivedAt,
        cursor.createdAt,
        cursor.id,
      );
    }
    bindings.push(limit + 1);
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
          ${cursorClause}
          ORDER BY m.received_at DESC, m.created_at DESC, m.id DESC
          LIMIT ?`,
      )
      .bind(...bindings)
      .all<AdminMessageRow>();
    return {
      rows: result.results.slice(0, limit),
      hasMore: result.results.length > limit,
    };
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
