import type { D1Migration } from "@cloudflare/vitest-pool-workers";
import type { Env as MailboxEnv } from "../src/shared/types";

declare module "cloudflare:test" {
  interface ProvidedEnv extends MailboxEnv {
    TEST_D1_MIGRATIONS: string;
  }
}

export type TestD1Migration = D1Migration;
