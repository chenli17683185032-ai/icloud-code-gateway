import { applyD1Migrations, env } from "cloudflare:test";
import type { D1Migration } from "@cloudflare/vitest-pool-workers";
import type { Env } from "../src/shared/types";

const testEnv = env as unknown as Env & { TEST_D1_MIGRATIONS: string };
await applyD1Migrations(
  testEnv.DB,
  JSON.parse(testEnv.TEST_D1_MIGRATIONS) as D1Migration[],
);
