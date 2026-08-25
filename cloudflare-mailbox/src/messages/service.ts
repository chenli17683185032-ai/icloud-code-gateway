import { AliasService } from "../aliases/service";
import { extractVerificationCode } from "../mail/extract-code";
import { classifyMessage, shouldPreserveMessage } from "../mail/classify";
import { parseIncomingEmail, type ParsedAttachment } from "../mail/parse";
import { AppError, NotFoundError } from "../shared/errors";
import {
  decryptBytes,
  decryptJson,
  encryptBytes,
  encryptJson,
  hmacDigest,
} from "../shared/security";
import type {
  AttachmentSecretPayload,
  AliasSecretPayload,
  MessageSecretPayload,
  RuntimeConfig,
} from "../shared/types";
import {
  type AttachmentRow,
  type AdminMessageRow,
  MessageRepository,
  type MessageRow,
} from "./repository";

export interface PublicMessage {
  id: string;
  sender: string;
  subject: string;
  body: string;
  code: string;
  receivedAt: string;
  expiresAt: string;
}

export interface PublicCodeMessage {
  id: string;
  code: string;
  receivedAt: string;
  expiresAt: string;
}

export interface AdminMessage extends PublicMessage {
  email: string;
  category: "gpt" | "grok" | "other";
  permanent: boolean;
  hasHtml: boolean;
  attachments: AttachmentSummary[];
}

export interface AttachmentSummary {
  id: string;
  filename: string;
  mimeType: string;
  size: number;
  inline: boolean;
}

export interface AttachmentDownload extends AttachmentSummary {
  bytes: ArrayBuffer;
}

export interface IncomingMessage {
  raw: ReadableStream;
  rawSize: number;
  envelopeFrom: string;
  envelopeTo: string;
}

const MIME_TYPE = /^[A-Za-z0-9!#$&^_.+-]+\/[A-Za-z0-9!#$&^_.+-]+$/;

function normalizedMimeType(value: string): string {
  const mimeType = value.trim().toLowerCase();
  return MIME_TYPE.test(mimeType) ? mimeType : "application/octet-stream";
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function bytesToBase64(value: ArrayBuffer): string {
  const bytes = new Uint8Array(value);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function privacySafeHtml(value: string): string {
  return value
    .replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>/gi, "")
    .replace(/<meta\b(?=[^>]*http-equiv\s*=\s*["']?refresh\b)[^>]*>/gi, "")
    .replace(/<(?:base|link)\b[^>]*>/gi, "")
    .replace(
      /\s(?:src|poster|background|srcset)\s*=\s*"[^"]*(?:https?:)?\/\/[^"]*"/gi,
      "",
    )
    .replace(
      /\s(?:src|poster|background|srcset)\s*=\s*'[^']*(?:https?:)?\/\/[^']*'/gi,
      "",
    )
    .replace(
      /\s(?:src|poster|background|srcset)\s*=\s*(?:https?:)?\/\/[^\s>]+/gi,
      "",
    )
    .replace(/url\(\s*["']?(?:https?:)?\/\/[^)]*\)/gi, "none")
    .replace(/@import\s+(?:url\()?\s*["']?(?:https?:)?\/\/[^;]+;/gi, "");
}

export class MessageService {
  constructor(
    private readonly repository: MessageRepository,
    private readonly aliases: AliasService,
    private readonly attachmentStore: KVNamespace,
    private readonly config: RuntimeConfig,
  ) {}

  private async decryptedMessage(
    row: MessageRow,
  ): Promise<PublicMessage & { html: string }> {
    const payload = await decryptJson<MessageSecretPayload>(
      this.config,
      { ciphertext: row.payload_ciphertext, iv: row.payload_iv },
      `message:${row.id}`,
    );
    return {
      id: row.id,
      sender: payload.sender,
      subject: payload.subject,
      body: payload.body,
      html: payload.html || "",
      code: payload.code,
      receivedAt: new Date(row.received_at * 1000).toISOString(),
      expiresAt: new Date(row.expires_at * 1000).toISOString(),
    };
  }

  private async attachmentSummary(
    row: AttachmentRow,
  ): Promise<AttachmentSummary> {
    const metadata = await this.attachmentMetadata(row);
    return {
      id: row.id,
      filename: metadata.filename,
      mimeType: normalizedMimeType(metadata.mimeType),
      size: row.size_bytes,
      inline: metadata.disposition === "inline",
    };
  }

  private attachmentMetadata(
    row: AttachmentRow,
  ): Promise<AttachmentSecretPayload> {
    return decryptJson<AttachmentSecretPayload>(
      this.config,
      {
        ciphertext: row.metadata_ciphertext,
        iv: row.metadata_iv,
      },
      `attachment:${row.id}`,
    );
  }

  private async storeAttachments(
    messageId: string,
    attachments: ParsedAttachment[],
    expiresAt: number,
    permanent: boolean,
    now: number,
  ): Promise<void> {
    for (const [index, attachment] of attachments.entries()) {
      const id = await hmacDigest(
        this.config.lookupHmacKey,
        `attachment\0${messageId}\0${index}`,
      );
      const objectKey = `mail/${messageId}/${id}`;
      const metadata: AttachmentSecretPayload = {
        filename: attachment.filename || `附件-${index + 1}`,
        mimeType: normalizedMimeType(attachment.mimeType),
        disposition: attachment.disposition,
        contentId: attachment.contentId,
      };
      const [encryptedMetadata, encryptedContent] = await Promise.all([
        encryptJson(this.config, metadata, `attachment:${id}`),
        encryptBytes(this.config, attachment.content, `attachment:${id}`),
      ]);
      await this.attachmentStore.put(
        objectKey,
        encryptedContent,
        permanent ? undefined : { expiration: expiresAt },
      );
      await this.repository.upsertAttachment({
        id,
        messageId,
        objectKey,
        metadataCiphertext: encryptedMetadata.ciphertext,
        metadataIv: encryptedMetadata.iv,
        sizeBytes: attachment.content.byteLength,
        createdAt: now,
      });
    }
  }

  async ingest(
    message: IncomingMessage,
    now = Math.floor(Date.now() / 1000),
  ): Promise<{ stored: number; matched: number; duplicate: number }> {
    if (message.rawSize > this.config.maxEmailBytes) {
      return { stored: 0, matched: 0, duplicate: 0 };
    }
    if (
      this.config.inboxAddress &&
      message.envelopeTo.trim().toLowerCase() !== this.config.inboxAddress
    ) {
      return { stored: 0, matched: 0, duplicate: 0 };
    }
    const parsed = await parseIncomingEmail(
      message.raw,
      this.config.maxBodyChars,
      this.config.maxHtmlChars,
    );
    const knownAliases: string[] = [];
    for (const recipient of parsed.recipients.slice(0, 24)) {
      try {
        const digest = await hmacDigest(
          this.config.lookupHmacKey,
          `alias\0${recipient}`,
        );
        const row = await this.aliases.getByDigest(digest);
        if (row?.state === "active") knownAliases.push(digest);
      } catch {
        continue;
      }
    }
    const uniqueAliases = [...new Set(knownAliases)];
    if (!uniqueAliases.length) return { stored: 0, matched: 0, duplicate: 0 };

    const payload: MessageSecretPayload = {
      sender: parsed.sender || message.envelopeFrom,
      subject: parsed.subject,
      body: parsed.body,
      html: parsed.html,
      code: extractVerificationCode(parsed.sender, parsed.subject, parsed.body),
    };
    const category = classifyMessage(
      parsed.sender,
      message.envelopeFrom,
      parsed.subject,
      parsed.body,
      payload.code,
    );
    const permanent = shouldPreserveMessage(
      category,
      payload.code,
      payload.subject,
      payload.body,
    );
    const identity =
      parsed.parsed.messageId ||
      `${message.envelopeFrom}\0${parsed.subject}\0${parsed.body.slice(0, 2048)}\0${
        parsed.parsed.date || ""
      }`;
    const messageDigest = await hmacDigest(
      this.config.lookupHmacKey,
      `message\0${identity}`,
    );
    let stored = 0;
    let duplicate = 0;
    for (const digest of uniqueAliases) {
      const id = crypto.randomUUID();
      const encrypted = await encryptJson(
        this.config,
        payload,
        `message:${id}`,
      );
      const expiresAt = permanent
        ? 253_402_300_799
        : now + this.config.emailRetentionSeconds;
      const result = await this.repository.insert({
        id,
        aliasDigest: digest,
        messageDigest,
        payloadCiphertext: encrypted.ciphertext,
        payloadIv: encrypted.iv,
        receivedAt: now,
        expiresAt,
        createdAt: now,
        category,
        hasCode: Boolean(payload.code),
        retentionClass: permanent ? "permanent" : "temporary",
      });
      await this.storeAttachments(
        result.id,
        parsed.attachments,
        expiresAt,
        permanent,
        now,
      );
      if (result.inserted) stored += 1;
      else duplicate += 1;
    }
    return { stored, matched: uniqueAliases.length, duplicate };
  }

  async listCodes(
    aliasDigest: string,
    limit: number,
    now = Math.floor(Date.now() / 1000),
  ) {
    const rows = await this.repository.listPublicCodes(aliasDigest, now, limit);
    const messages = await Promise.all(
      rows.map((row) => this.decryptedMessage(row)),
    );
    return messages.map(
      ({ id, code, receivedAt, expiresAt }): PublicCodeMessage => ({
        id,
        code,
        receivedAt,
        expiresAt,
      }),
    );
  }

  async listAll(
    limit: number,
    now = Math.floor(Date.now() / 1000),
  ): Promise<AdminMessage[]> {
    const rows = await this.repository.listAll(now, limit);
    const attachmentRows = await this.repository.listAttachments(
      rows.map((row) => row.id),
    );
    const grouped = new Map<string, AttachmentRow[]>();
    for (const attachment of attachmentRows) {
      const values = grouped.get(attachment.message_id) ?? [];
      values.push(attachment);
      grouped.set(attachment.message_id, values);
    }
    return Promise.all(
      rows.map((row) => this.adminMessage(row, grouped.get(row.id) ?? [])),
    );
  }

  private async adminMessage(
    row: AdminMessageRow,
    attachments: AttachmentRow[],
  ): Promise<AdminMessage> {
    const [message, alias, attachmentSummaries] = await Promise.all([
      this.decryptedMessage(row),
      decryptJson<AliasSecretPayload>(
        this.config,
        {
          ciphertext: row.alias_secret_ciphertext,
          iv: row.alias_secret_iv,
        },
        `alias:${row.alias_digest}`,
      ),
      Promise.all(attachments.map((item) => this.attachmentSummary(item))),
    ]);
    const { html, ...publicMessage } = message;
    return {
      ...publicMessage,
      email: alias.email,
      category: row.category,
      permanent: row.retention_class === "permanent",
      hasHtml: Boolean(html),
      attachments: attachmentSummaries,
    };
  }

  async htmlDocument(
    messageId: string,
    now = Math.floor(Date.now() / 1000),
  ): Promise<string> {
    const row = await this.repository.getById(messageId, now);
    if (!row) throw new NotFoundError("邮件不存在或已过期。");
    const message = await this.decryptedMessage(row);
    if (message.html) {
      let html = privacySafeHtml(message.html);
      const attachments = await this.repository.listAttachments([messageId]);
      const metadataRows = await Promise.all(
        attachments.map(async (attachment) => ({
          attachment,
          metadata: await this.attachmentMetadata(attachment),
        })),
      );
      let inlineBudget = 2_000_000;
      const inlineImages = metadataRows.filter(({ attachment, metadata }) => {
        const contentId = metadata.contentId.replace(/^<|>$/g, "").trim();
        const mimeType = normalizedMimeType(metadata.mimeType);
        if (
          !contentId ||
          metadata.disposition !== "inline" ||
          !mimeType.startsWith("image/") ||
          attachment.size_bytes > 1_500_000 ||
          attachment.size_bytes > inlineBudget
        ) {
          return false;
        }
        inlineBudget -= attachment.size_bytes;
        return true;
      });
      const replacements = await Promise.all(
        inlineImages.map(async ({ attachment, metadata }) => {
          const contentId = metadata.contentId.replace(/^<|>$/g, "").trim();
          const mimeType = normalizedMimeType(metadata.mimeType);
          const encrypted = await this.attachmentStore.get(
            attachment.object_key,
            "arrayBuffer",
          );
          if (!encrypted) return null;
          const plaintext = await decryptBytes(
            this.config,
            encrypted,
            `attachment:${attachment.id}`,
          );
          return {
            contentId,
            dataUrl: `data:${mimeType};base64,${bytesToBase64(plaintext)}`,
          };
        }),
      );
      for (const replacement of replacements) {
        if (!replacement) continue;
        html = html.replace(
          new RegExp(`cid:${escapeRegExp(replacement.contentId)}`, "gi"),
          replacement.dataUrl,
        );
      }
      return html;
    }
    return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>body{margin:0;padding:24px;color:#18211d;background:#fff;font:15px/1.75 system-ui,sans-serif}pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere}</style></head><body><pre>${escapeHtml(message.body || "这封邮件没有可显示的正文。")}</pre></body></html>`;
  }

  async attachment(
    messageId: string,
    attachmentId: string,
    now = Math.floor(Date.now() / 1000),
  ): Promise<AttachmentDownload> {
    const row = await this.repository.getAttachment(
      messageId,
      attachmentId,
      now,
    );
    if (!row) throw new NotFoundError("附件不存在或已过期。");
    const [summary, encrypted] = await Promise.all([
      this.attachmentSummary(row),
      this.attachmentStore.get(row.object_key, "arrayBuffer"),
    ]);
    if (!encrypted) {
      throw new AppError(
        "attachment_pending",
        503,
        "附件正在同步，请稍后重试。",
      );
    }
    return {
      ...summary,
      bytes: await decryptBytes(
        this.config,
        encrypted,
        `attachment:${attachmentId}`,
      ),
    };
  }

  private async removeAttachments(rows: AttachmentRow[]): Promise<void> {
    if (!rows.length) return;
    await Promise.all(
      rows.map((attachment) =>
        this.attachmentStore.delete(attachment.object_key),
      ),
    );
    await this.repository.deleteAttachmentRows(rows.map((row) => row.id));
  }

  async cleanup(now = Math.floor(Date.now() / 1000)): Promise<number> {
    for (;;) {
      const expired = await this.repository.listExpiredAttachments(now);
      if (!expired.length) break;
      await this.removeAttachments(expired);
      if (expired.length < 100) break;
    }
    const removed = await this.repository.cleanup(now);
    for (;;) {
      const orphans = await this.repository.listOrphanAttachments();
      if (!orphans.length) break;
      await this.removeAttachments(orphans);
      if (orphans.length < 100) break;
    }
    return removed;
  }
}
