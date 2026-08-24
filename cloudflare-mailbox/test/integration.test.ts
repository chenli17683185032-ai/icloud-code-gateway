import { SELF, env } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";
import worker from "../src/index";
import type { Env } from "../src/shared/types";

const token = `icg_${"b".repeat(43)}`;
const operatorToken = `icg_${"o".repeat(43)}`;
const controlHeaders = {
  Authorization: "Bearer control-token-abcdefghijklmnop",
  "Content-Type": "application/json",
};
const mailboxEnv = env as unknown as Env;

beforeEach(async () => {
  await mailboxEnv.DB.batch([
    mailboxEnv.DB.prepare("DELETE FROM messages"),
    mailboxEnv.DB.prepare("DELETE FROM aliases"),
    mailboxEnv.DB.prepare("DELETE FROM auth_rate_limits"),
  ]);
});

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

function emailMessage(raw: string): ForwardableEmailMessage {
  return {
    from: raw.includes("tm.openai.com")
      ? "noreply@tm.openai.com"
      : "sender@example.com",
    to: "otp@example.com",
    raw: new Response(raw).body,
    rawSize: raw.length,
  } as ForwardableEmailMessage;
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

    const adminPage = await SELF.fetch("https://example.com/admin/");
    expect(adminPage.status).toBe(200);
    expect(await adminPage.text()).toContain("隐邮操作台");

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

  it("opens historical key-only links without requiring the email", async () => {
    await upsertAlias();
    const response = await SELF.fetch("https://example.com/api/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": "198.51.100.9",
      },
      body: JSON.stringify({ token }),
    });
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      mailbox: { email: "hidden.one@icloud.com" },
    });
  });

  it("returns only GPT/Grok codes to regular users and deduplicates mail", async () => {
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
    await worker.email?.(emailMessage(raw), mailboxEnv, {} as ExecutionContext);
    await worker.email?.(emailMessage(raw), mailboxEnv, {} as ExecutionContext);

    const cookie = await createSession();
    const response = await SELF.fetch("https://example.com/api/messages", {
      headers: { Cookie: cookie },
    });
    const payload = (await response.json()) as {
      messages: Array<Record<string, unknown> & { code: string }>;
    };
    expect(payload.messages).toHaveLength(1);
    expect(payload.messages[0]).toMatchObject({ code: "654321" });
    expect(payload.messages[0]).not.toHaveProperty("body");
    expect(payload.messages[0]).not.toHaveProperty("subject");
    expect(payload.messages[0]).not.toHaveProperty("sender");
  });

  it("hides other mail from users while the operator sees all and retention", async () => {
    await upsertAlias();
    const newsletter = [
      "From: Sender <sender@example.com>",
      "To: hidden.one@icloud.com",
      "X-Original-To: hidden.one@icloud.com",
      "Subject: Weekly newsletter",
      "Message-ID: <newsletter@example.com>",
      "Content-Type: text/plain; charset=utf-8",
      "",
      "General product news without a verification code.",
    ].join("\r\n");
    const support = [
      "From: Support <support@example.com>",
      "To: hidden.one@icloud.com",
      "X-Original-To: hidden.one@icloud.com",
      "Subject: 售后支持工单已更新",
      "Message-ID: <support-case@example.com>",
      "Content-Type: text/plain; charset=utf-8",
      "",
      "你的申诉工单正在处理中。",
    ].join("\r\n");
    await worker.email?.(
      emailMessage(newsletter),
      mailboxEnv,
      {} as ExecutionContext,
    );
    await worker.email?.(
      emailMessage(support),
      mailboxEnv,
      {} as ExecutionContext,
    );

    const userCookie = await createSession();
    const userResponse = await SELF.fetch("https://example.com/api/messages", {
      headers: { Cookie: userCookie },
    });
    await expect(userResponse.json()).resolves.toMatchObject({ messages: [] });

    const login = await SELF.fetch("https://example.com/api/operator/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": "198.51.100.20",
      },
      body: JSON.stringify({ token: operatorToken }),
    });
    expect(login.status).toBe(200);
    const operatorCookie =
      login.headers.get("Set-Cookie")?.split(";", 1)[0] ?? "";
    const operatorResponse = await SELF.fetch(
      "https://example.com/api/operator/messages",
      { headers: { Cookie: operatorCookie } },
    );
    const operatorPayload = (await operatorResponse.json()) as {
      messages: Array<{
        email: string;
        subject: string;
        body: string;
        permanent: boolean;
      }>;
    };
    expect(operatorPayload.messages).toHaveLength(2);
    expect(operatorPayload.messages).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          email: "hidden.one@icloud.com",
          subject: "售后支持工单已更新",
          body: "你的申诉工单正在处理中。",
          permanent: true,
        }),
        expect.objectContaining({
          subject: "Weekly newsletter",
          permanent: false,
        }),
      ]),
    );
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
