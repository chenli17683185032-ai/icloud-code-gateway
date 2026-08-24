import { SELF, env } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/shared/types";

const token = `icg_${"b".repeat(43)}`;
const controlHeaders = {
  Authorization: "Bearer control-token-abcdefghijklmnop",
  "Content-Type": "application/json",
};
const mailboxEnv = env as unknown as Env;

async function upsertAlias(): Promise<void> {
  const response = await SELF.fetch("https://example.com/control/v1/aliases", {
    method: "POST",
    headers: controlHeaders,
    body: JSON.stringify({
      id: "local-1",
      email: "hidden.one@icloud.com",
      label: "one",
      state: "active",
      access_key: token,
    }),
  });
  expect(response.status).toBe(200);
}

async function createSession(): Promise<string> {
  const response = await SELF.fetch("https://example.com/api/session", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "CF-Connecting-IP": "198.51.100.8",
    },
    body: JSON.stringify({ email: "hidden.one@icloud.com", token }),
  });
  expect(response.status).toBe(200);
  return response.headers.get("Set-Cookie")?.split(";", 1)[0] ?? "";
}

describe("worker integration", () => {
  it("serves the shared operator-and-user mailbox without browser storage", async () => {
    const page = await SELF.fetch("https://example.com/");
    expect(page.status).toBe(200);
    expect(page.headers.get("Content-Security-Policy")).toContain(
      "default-src 'self'",
    );
    const html = await page.text();
    expect(html).toContain("隐邮收件箱");

    const script = await (
      await SELF.fetch("https://example.com/app.js")
    ).text();
    expect(script).not.toContain("localStorage");
    expect(script).not.toContain("sessionStorage");

    const session = await SELF.fetch("https://example.com/api/session");
    await expect(session.json()).resolves.toMatchObject({
      authenticated: false,
    });
  });

  it("keeps the existing control-plane contract and serves an empty mailbox", async () => {
    await upsertAlias();
    const cookie = await createSession();
    const response = await SELF.fetch("https://example.com/api/messages", {
      headers: { Cookie: cookie },
    });
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      mailbox: { email: "hidden.one@icloud.com" },
      messages: [],
    });
  });

  it("ingests one forwarded email, returns body and deduplicates it", async () => {
    await upsertAlias();
    const raw = [
      "From: OpenAI <noreply@tm.openai.com>",
      "To: hidden.one@icloud.com",
      "X-Apple-Original-Recipient: hidden.one@icloud.com",
      "Subject: Your verification code",
      "Message-ID: <integration-one@example.com>",
      "Content-Type: text/plain; charset=utf-8",
      "",
      "Your verification code is 654321.",
    ].join("\r\n");
    const emailMessage = () =>
      ({
        from: "noreply@tm.openai.com",
        to: "otp@example.com",
        raw: new Response(raw).body,
        rawSize: raw.length,
      }) as ForwardableEmailMessage;
    await worker.email?.(emailMessage(), mailboxEnv, {} as ExecutionContext);
    await worker.email?.(emailMessage(), mailboxEnv, {} as ExecutionContext);

    const cookie = await createSession();
    const response = await SELF.fetch("https://example.com/api/messages", {
      headers: { Cookie: cookie },
    });
    const payload = (await response.json()) as {
      messages: Array<{ body: string; code: string }>;
    };
    expect(payload.messages).toHaveLength(1);
    expect(payload.messages[0]).toMatchObject({
      code: "654321",
      body: "Your verification code is 654321.",
    });
  });

  it("rejects invalid control tokens and invalidates sessions after key rotation", async () => {
    const denied = await SELF.fetch("https://example.com/control/v1/aliases", {
      method: "POST",
      headers: { ...controlHeaders, Authorization: "Bearer wrong" },
      body: JSON.stringify({ email: "hidden.one@icloud.com" }),
    });
    expect(denied.status).toBe(401);

    await upsertAlias();
    const cookie = await createSession();
    const nextToken = `icg_${"c".repeat(43)}`;
    const rotated = await SELF.fetch(
      "https://example.com/control/v1/aliases/by-email/hidden.one%40icloud.com/key",
      {
        method: "POST",
        headers: controlHeaders,
        body: JSON.stringify({ access_key: nextToken, id: "local-1" }),
      },
    );
    expect(rotated.status).toBe(200);
    const staleSession = await SELF.fetch("https://example.com/api/messages", {
      headers: { Cookie: cookie },
    });
    expect(staleSession.status).toBe(401);
  });
});
