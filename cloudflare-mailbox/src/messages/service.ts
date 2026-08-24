import { AliasService } from "../aliases/service";
import { extractVerificationCode } from "../mail/extract-code";
import { parseIncomingEmail } from "../mail/parse";
import { decryptJson, encryptJson, hmacDigest } from "../shared/security";
import type { MessageSecretPayload, RuntimeConfig } from "../shared/types";
import { MessageRepository, type MessageRow } from "./repository";

export interface PublicMessage {
  id: string;
  sender: string;
  subject: string;
  body: string;
  code: string;
  receivedAt: string;
  expiresAt: string;
}

export interface IncomingMessage {
  raw: ReadableStream;
  rawSize: number;
  envelopeFrom: string;
  envelopeTo: string;
}

export class MessageService {
  constructor(
    private readonly repository: MessageRepository,
    private readonly aliases: AliasService,
    private readonly config: RuntimeConfig,
  ) {}

  private async publicMessage(row: MessageRow): Promise<PublicMessage> {
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
      code: payload.code,
      receivedAt: new Date(row.received_at * 1000).toISOString(),
      expiresAt: new Date(row.expires_at * 1000).toISOString(),
    };
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
      code: extractVerificationCode(parsed.sender, parsed.subject, parsed.body),
    };
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
      const inserted = await this.repository.insert({
        id,
        aliasDigest: digest,
        messageDigest,
        payloadCiphertext: encrypted.ciphertext,
        payloadIv: encrypted.iv,
        receivedAt: now,
        expiresAt: now + this.config.emailRetentionSeconds,
        createdAt: now,
      });
      if (inserted) stored += 1;
      else duplicate += 1;
    }
    return { stored, matched: uniqueAliases.length, duplicate };
  }

  async list(
    aliasDigest: string,
    limit: number,
    now = Math.floor(Date.now() / 1000),
  ) {
    const rows = await this.repository.list(aliasDigest, now, limit);
    return Promise.all(rows.map((row) => this.publicMessage(row)));
  }

  cleanup(now = Math.floor(Date.now() / 1000)): Promise<number> {
    return this.repository.cleanup(now);
  }
}
