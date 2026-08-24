import { NotFoundError, UnauthorizedError } from "../shared/errors";
import {
  accessTokenDigest,
  aliasDigest,
  decryptJson,
  encryptJson,
  normalizeEmail,
  safeEqual,
} from "../shared/security";
import type {
  AliasSecretPayload,
  RuntimeConfig,
  SessionPayload,
} from "../shared/types";
import { AliasRepository, type AliasRow } from "./repository";
import type { ControlAliasInput } from "./schema";

export interface AliasResult {
  id: string;
  email: string;
  state: "active" | "inactive";
  hasAccessKey: boolean;
}

export interface AuthenticatedAlias {
  row: AliasRow;
  secret: AliasSecretPayload;
}

export class AliasService {
  constructor(
    private readonly repository: AliasRepository,
    private readonly config: RuntimeConfig,
  ) {}

  private async secretFor(row: AliasRow): Promise<AliasSecretPayload> {
    return decryptJson<AliasSecretPayload>(
      this.config,
      { ciphertext: row.secret_ciphertext, iv: row.secret_iv },
      `alias:${row.alias_digest}`,
    );
  }

  private result(row: AliasRow, email: string): AliasResult {
    return {
      id: row.external_id || row.alias_digest.slice(0, 24),
      email,
      state: row.state,
      hasAccessKey: Boolean(row.token_digest),
    };
  }

  async upsert(
    input: ControlAliasInput,
    now = Math.floor(Date.now() / 1000),
  ): Promise<AliasResult> {
    const email = normalizeEmail(input.email);
    const digest = await aliasDigest(this.config, email);
    const existing = await this.repository.getByDigest(digest);
    const state = input.state;
    let tokenDigest = existing?.token_digest ?? null;
    if (state === "inactive") tokenDigest = null;
    else if (input.access_key) {
      tokenDigest = await accessTokenDigest(
        this.config,
        digest,
        input.access_key,
      );
    }
    const secret: AliasSecretPayload = {
      email,
      label: input.label || email,
      note: input.note,
      senderFilter: input.sender_filter,
    };
    const encrypted = await encryptJson(this.config, secret, `alias:${digest}`);
    await this.repository.upsert({
      aliasDigest: digest,
      externalId: input.id,
      secretCiphertext: encrypted.ciphertext,
      secretIv: encrypted.iv,
      tokenDigest,
      state,
      now,
    });
    const row = await this.requireByDigest(digest);
    return this.result(row, email);
  }

  async issueKey(
    emailValue: string,
    accessKey: string,
    externalId = "",
    now = Math.floor(Date.now() / 1000),
  ): Promise<AliasResult> {
    const email = normalizeEmail(emailValue);
    const digest = await aliasDigest(this.config, email);
    let row = await this.repository.getByDigest(digest);
    if (!row) {
      await this.upsert({
        id: externalId,
        email,
        label: email,
        note: "",
        sender_filter: "",
        state: "active",
        access_key: accessKey,
      });
    } else {
      const secret = await this.secretFor(row);
      const encrypted = await encryptJson(
        this.config,
        secret,
        `alias:${digest}`,
      );
      await this.repository.upsert({
        aliasDigest: digest,
        externalId: externalId || row.external_id,
        secretCiphertext: encrypted.ciphertext,
        secretIv: encrypted.iv,
        tokenDigest: await accessTokenDigest(this.config, digest, accessKey),
        state: "active",
        now,
      });
    }
    row = await this.requireByDigest(digest);
    return this.result(row, email);
  }

  async revokeKey(
    emailValue: string,
    now = Math.floor(Date.now() / 1000),
  ): Promise<void> {
    const digest = await aliasDigest(this.config, normalizeEmail(emailValue));
    await this.requireByDigest(digest);
    await this.repository.setTokenDigest(digest, null, now);
  }

  async setState(
    emailValue: string,
    state: "active" | "inactive",
    now = Math.floor(Date.now() / 1000),
  ): Promise<AliasResult> {
    const email = normalizeEmail(emailValue);
    const digest = await aliasDigest(this.config, email);
    const existing = await this.repository.getByDigest(digest);
    const existingSecret = existing ? await this.secretFor(existing) : null;
    return this.upsert(
      {
        id: existing?.external_id ?? "",
        email,
        label: existingSecret?.label ?? email,
        note: existingSecret?.note ?? "",
        sender_filter: existingSecret?.senderFilter ?? "",
        state,
        access_key: "",
      },
      now,
    );
  }

  async delete(emailValue: string): Promise<void> {
    const digest = await aliasDigest(this.config, normalizeEmail(emailValue));
    await this.requireByDigest(digest);
    await this.repository.deleteByDigest(digest);
  }

  async authenticate(
    emailValue: string,
    token: string,
  ): Promise<AuthenticatedAlias> {
    const email = normalizeEmail(emailValue);
    const digest = await aliasDigest(this.config, email);
    const row = await this.repository.getByDigest(digest);
    if (!row || row.state !== "active" || !row.token_digest)
      throw new UnauthorizedError();
    const supplied = await accessTokenDigest(this.config, digest, token);
    if (!(await safeEqual(row.token_digest, supplied)))
      throw new UnauthorizedError();
    return { row, secret: await this.secretFor(row) };
  }

  async authenticateSession(
    session: SessionPayload,
  ): Promise<AuthenticatedAlias> {
    const row = await this.repository.getByDigest(session.aliasDigest);
    if (
      !row ||
      row.state !== "active" ||
      !row.token_digest ||
      !(await safeEqual(row.token_digest, session.tokenDigest))
    ) {
      throw new UnauthorizedError("会话已失效，请重新查询。");
    }
    return { row, secret: await this.secretFor(row) };
  }

  getByDigest(aliasDigestValue: string): Promise<AliasRow | null> {
    return this.repository.getByDigest(aliasDigestValue);
  }

  private async requireByDigest(digest: string): Promise<AliasRow> {
    const row = await this.repository.getByDigest(digest);
    if (!row) throw new NotFoundError("邮箱尚未同步。");
    return row;
  }
}
