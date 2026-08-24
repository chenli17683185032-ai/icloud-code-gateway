export class RateLimitRepository {
  constructor(private readonly database: D1Database) {}

  async consume(fingerprint: string, now: number): Promise<number> {
    const windowStarted = Math.floor(now / 60) * 60;
    await this.database
      .prepare(
        `INSERT INTO auth_rate_limits (fingerprint, window_started, attempts, updated_at)
         VALUES (?, ?, 1, ?)
         ON CONFLICT(fingerprint, window_started) DO UPDATE SET
            attempts = attempts + 1,
            updated_at = excluded.updated_at`,
      )
      .bind(fingerprint, windowStarted, now)
      .run();
    const row = await this.database
      .prepare(
        `SELECT attempts
           FROM auth_rate_limits
          WHERE fingerprint = ? AND window_started = ?`,
      )
      .bind(fingerprint, windowStarted)
      .first<{ attempts: number }>();
    return Number(row?.attempts ?? 1);
  }

  async cleanup(before: number): Promise<void> {
    await this.database
      .prepare("DELETE FROM auth_rate_limits WHERE updated_at < ?")
      .bind(before)
      .run();
  }
}
