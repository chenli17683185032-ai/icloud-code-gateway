import { env } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";
import {
  MessageRepository,
  type AdminMessageCursor,
} from "../src/messages/repository";
import type { Env } from "../src/shared/types";

const database = (env as unknown as Env).DB;
const now = 1_700_000_000;

interface CapturedQuery {
  sql: string;
  bindings: Array<string | number>;
}

interface FixtureMessage {
  id: string;
  receivedAt?: number;
  createdAt?: number;
  expiresAt?: number;
  retention?: "permanent" | "temporary";
}

let captured: CapturedQuery[];
let repository: MessageRepository;

beforeEach(async () => {
  await database.batch([
    database.prepare("DELETE FROM message_attachments"),
    database.prepare("DELETE FROM messages"),
    database.prepare("DELETE FROM aliases"),
  ]);
  await database
    .prepare(
      `INSERT INTO aliases (
         alias_digest, external_id, secret_ciphertext, secret_iv,
         state, created_at, updated_at
       ) VALUES ('fixture-alias', 'fixture', 'ciphertext', 'iv', 'active', ?, ?)`,
    )
    .bind(now, now)
    .run();
  captured = [];
  // Capture the repository's real SQL and bindings while executing against the
  // local D1 binding. Plan assertions must not drift into testing copied SQL.
  const capturingDatabase = {
    prepare(sql: string) {
      const statement = database.prepare(sql);
      return {
        bind(...bindings: Array<string | number>) {
          captured.push({ sql, bindings });
          return statement.bind(...bindings);
        },
      };
    },
  } as unknown as D1Database;
  repository = new MessageRepository(capturingDatabase);
});

async function insertMessages(messages: FixtureMessage[]): Promise<void> {
  await database.batch(
    messages.map((message) =>
      database
        .prepare(
          `INSERT INTO messages (
             id, alias_digest, message_digest, payload_ciphertext, payload_iv,
             received_at, expires_at, created_at, category, has_code, retention_class
           ) VALUES (?, 'fixture-alias', ?, 'ciphertext', 'iv', ?, ?, ?, 'gpt', 1, ?)`,
        )
        .bind(
          message.id,
          `digest-${message.id}`,
          message.receivedAt ?? now - 10,
          message.expiresAt ?? now + 60,
          message.createdAt ?? now - 10,
          message.retention ?? "permanent",
        ),
    ),
  );
}

async function explainLastQuery(): Promise<string[]> {
  const query = captured.at(-1);
  expect(query).toBeDefined();
  const result = await database
    .prepare(`EXPLAIN QUERY PLAN ${query!.sql}`)
    .bind(...query!.bindings)
    .all<{ detail: string }>();
  return result.results.map((row) => row.detail);
}

describe("operator archive query plans", () => {
  it("reads the first page in index order without sorting the full archive", async () => {
    await insertMessages(
      Array.from({ length: 120 }, (_, index) => ({
        id: `message-${String(index).padStart(3, "0")}`,
        receivedAt: now - index,
        createdAt: now - index,
      })),
    );
    const page = await repository.listAll(now, 20);
    expect(page.rows).toHaveLength(20);
    expect(page.hasMore).toBe(true);
    expect(page.rows[0]?.id).toBe("message-000");
    const details = await explainLastQuery();
    expect(
      details.some((detail) => detail.includes("messages_admin_received_idx")),
    ).toBe(true);
    expect(details.join("\n")).not.toMatch(/TEMP B-TREE/i);
  });

  it("seeks directly to a deep cursor using the composite index range", async () => {
    await insertMessages(
      Array.from({ length: 120 }, (_, index) => ({
        id: `message-${String(index).padStart(3, "0")}`,
        receivedAt: now - index,
        createdAt: now - index,
      })),
    );
    const page = await repository.listAll(now, 5, {
      receivedAt: now - 100,
      createdAt: now - 100,
      id: "message-100",
    });
    expect(page.rows.map((row) => row.id)).toEqual([
      "message-101",
      "message-102",
      "message-103",
      "message-104",
      "message-105",
    ]);
    const details = await explainLastQuery();
    const archivePlan = details.find((detail) =>
      detail.includes("messages_admin_received_idx"),
    );
    expect(archivePlan).toMatch(/SEARCH .*messages_admin_received_idx/i);
    expect(archivePlan).toContain("<");
    expect(details.join("\n")).not.toMatch(/TEMP B-TREE/i);
  });

  it("paginates tied timestamps without gaps while honoring archive retention", async () => {
    const visibleIds = [
      "permanent-g",
      "permanent-f",
      "permanent-e",
      "permanent-d",
      "permanent-c",
      "permanent-b",
      "permanent-a",
      "active-b",
      "active-a",
    ];
    await insertMessages([
      ...visibleIds.map((id) => ({
        id,
        // Legacy permanent rows remain visible even when their old temporary
        // expiry predates now. Active temporary rows share the same sort keys.
        expiresAt: id.startsWith("permanent") ? 1 : now + 60,
        retention: id.startsWith("permanent")
          ? ("permanent" as const)
          : ("temporary" as const),
      })),
      { id: "expired-z", expiresAt: now, retention: "temporary" },
      { id: "expired-a", expiresAt: 1, retention: "temporary" },
    ]);
    const actual: string[] = [];
    let cursor: AdminMessageCursor | null = null;
    for (let pageNumber = 0; pageNumber < 4; pageNumber += 1) {
      const page = await repository.listAll(now, 3, cursor);
      actual.push(...page.rows.map((row) => row.id));
      if (!page.hasMore) break;
      const last = page.rows.at(-1)!;
      cursor = {
        receivedAt: last.received_at,
        createdAt: last.created_at,
        id: last.id,
      };
    }
    expect(actual).toEqual(visibleIds);
    expect(new Set(actual).size).toBe(visibleIds.length);
    expect(captured).toHaveLength(3);
  });
});
