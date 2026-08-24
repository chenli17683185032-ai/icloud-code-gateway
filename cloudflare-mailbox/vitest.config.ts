import {
  cloudflareTest,
  readD1Migrations,
} from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

const migrations = await readD1Migrations("./migrations");

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: "./wrangler.jsonc" },
      miniflare: {
        bindings: {
          TEST_D1_MIGRATIONS: JSON.stringify(migrations),
          CONTROL_PLANE_TOKEN: "control-token-abcdefghijklmnop",
          LOOKUP_HMAC_KEY: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
          DATA_ENCRYPTION_KEY: "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=",
          SESSION_SIGNING_KEY: "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI=",
          OPERATOR_ACCESS_TOKEN: `icg_${"o".repeat(43)}`,
          EMAIL_RETENTION_SECONDS: "86400",
          SESSION_TTL_SECONDS: "900",
          MESSAGE_QUERY_LIMIT: "20",
          MAX_BODY_CHARS: "50000",
          MAX_EMAIL_BYTES: "5000000",
          AUTH_ATTEMPTS_PER_MINUTE: "10",
          INBOX_ADDRESS: "otp@example.com",
        },
      },
    }),
  ],
  test: {
    setupFiles: ["./test/setup.ts"],
  },
});
