export interface AliasRow {
  alias_digest: string;
  external_id: string;
  secret_ciphertext: string;
  secret_iv: string;
  token_digest: string | null;
  state: "active" | "inactive";
  created_at: number;
  updated_at: number;
}

export interface AliasWrite {
  aliasDigest: string;
  externalId: string;
  secretCiphertext: string;
  secretIv: string;
  tokenDigest: string | null;
  state: "active" | "inactive";
  now: number;
}

export class AliasRepository {
  constructor(private readonly database: D1Database) {}

  getByDigest(aliasDigest: string): Promise<AliasRow | null> {
    return this.database
      .prepare(
        `SELECT alias_digest, external_id, secret_ciphertext, secret_iv,
                token_digest, state, created_at, updated_at
           FROM aliases
          WHERE alias_digest = ?`,
      )
      .bind(aliasDigest)
      .first<AliasRow>();
  }

  async upsert(value: AliasWrite): Promise<void> {
    await this.database
      .prepare(
        `INSERT INTO aliases (
            alias_digest, external_id, secret_ciphertext, secret_iv,
            token_digest, state, created_at, updated_at
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(alias_digest) DO UPDATE SET
            external_id = excluded.external_id,
            secret_ciphertext = excluded.secret_ciphertext,
            secret_iv = excluded.secret_iv,
            token_digest = excluded.token_digest,
            state = excluded.state,
            updated_at = excluded.updated_at`,
      )
      .bind(
        value.aliasDigest,
        value.externalId,
        value.secretCiphertext,
        value.secretIv,
        value.tokenDigest,
        value.state,
        value.now,
        value.now,
      )
      .run();
  }

  async setTokenDigest(
    aliasDigest: string,
    tokenDigest: string | null,
    now: number,
  ): Promise<void> {
    await this.database
      .prepare(
        `UPDATE aliases
            SET token_digest = ?, state = 'active', updated_at = ?
          WHERE alias_digest = ?`,
      )
      .bind(tokenDigest, now, aliasDigest)
      .run();
  }

  async deleteByDigest(aliasDigest: string): Promise<void> {
    await this.database.batch([
      this.database
        .prepare("DELETE FROM messages WHERE alias_digest = ?")
        .bind(aliasDigest),
      this.database
        .prepare("DELETE FROM aliases WHERE alias_digest = ?")
        .bind(aliasDigest),
    ]);
  }
}
