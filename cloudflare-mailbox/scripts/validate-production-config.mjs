import { readFile } from "node:fs/promises";

const source = await readFile(
  new URL("../wrangler.jsonc", import.meta.url),
  "utf8",
);
const databaseId = source.match(/"database_id"\s*:\s*"([^"]+)"/)?.[1] ?? "";
const inboxAddress = source.match(/"INBOX_ADDRESS"\s*:\s*"([^"]*)"/)?.[1] ?? "";

if (!databaseId || databaseId === "00000000-0000-0000-0000-000000000000") {
  throw new Error(
    "Replace the D1 database_id placeholder in wrangler.jsonc before deploying.",
  );
}
if (!inboxAddress || !inboxAddress.includes("@")) {
  throw new Error(
    "Set the production INBOX_ADDRESS in wrangler.jsonc before deploying.",
  );
}

console.info("Production Wrangler configuration is ready.");
