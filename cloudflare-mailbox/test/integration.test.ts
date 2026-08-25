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
  const attachments = await mailboxEnv.ATTACHMENTS.list({ prefix: "mail/" });
  await Promise.all(
    attachments.keys.map((item) => mailboxEnv.ATTACHMENTS.delete(item.name)),
  );
  await mailboxEnv.DB.batch([
    mailboxEnv.DB.prepare("DELETE FROM message_attachments"),
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

async function createOperatorSession(): Promise<string> {
  const response = await SELF.fetch(
    "https://example.com/api/operator/session",
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": "198.51.100.20",
      },
      body: JSON.stringify({ token: operatorToken }),
    },
  );
  expect(response.status).toBe(200);
  return response.headers.get("Set-Cookie")?.split(";", 1)[0] ?? "";
}

function emailMessage(raw: string, from?: string): ForwardableEmailMessage {
  return {
    from:
      from ??
      (raw.includes("tm.openai.com")
        ? "noreply@tm.openai.com"
        : "sender@example.com"),
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

    const stylesheet = await (
      await SELF.fetch("https://example.com/app.css")
    ).text();
    expect(stylesheet).toContain("#operator-view .message-row {");
    expect(stylesheet).toContain("min-height: 72px;");
    expect(stylesheet).toContain("#operator-view .message-preview {");
    expect(stylesheet).toContain(
      "height: clamp(520px, calc(100dvh - 430px), 720px);",
    );

    const adminPage = await SELF.fetch("https://example.com/admin/");
    expect(adminPage.status).toBe(200);
    const adminHtml = await adminPage.text();
    expect(adminHtml).toContain("隐邮操作台");
    expect(adminHtml).toContain('id="operator-search-input"');
    const adminScript = await (
      await SELF.fetch("https://example.com/admin/app.js")
    ).text();
    expect(adminScript).toContain('classList.add("operator-active")');
    expect(adminScript).toContain('classList.remove("operator-active")');
    expect(adminScript).toContain("async function loadAllMessages");
    expect(adminScript).toContain("fetchMessagePage");
    expect(adminScript).toContain("messageSearchText");
    expect(stylesheet).toContain("body.operator-active .page-shell {");
    expect(stylesheet).toContain(
      "width: min(1800px, calc(100% - clamp(1rem, 2vw, 2rem)));",
    );
    expect(stylesheet).toContain("height: calc(100dvh - 178px);");
    expect(stylesheet).toContain(
      "grid-template-columns: clamp(300px, 32%, 400px) minmax(0, 1fr);",
    );

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
      "From: OpenAI <relay-message@icloud.com>",
      "To: hidden.one@icloud.com",
      "X-Apple-Original-Recipient: hidden.one@icloud.com",
      "Subject: Your ChatGPT verification code",
      "Message-ID: <integration-one@example.com>",
      "Content-Type: text/html; charset=utf-8",
      "",
      "<p>Your OpenAI verification code is " +
        "<span>6</span><span>5</span><span>4</span>" +
        "<span>3</span><span>2</span><span>1</span>.</p>",
    ].join("\r\n");
    await worker.email?.(
      emailMessage(raw, "relay-message@icloud.com"),
      mailboxEnv,
      {} as ExecutionContext,
    );
    await worker.email?.(
      emailMessage(raw, "relay-message@icloud.com"),
      mailboxEnv,
      {} as ExecutionContext,
    );

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
    expect(payload.messages[0]).not.toHaveProperty("html");
    expect(payload.messages[0]).not.toHaveProperty("attachments");
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

    const operatorCookie = await createOperatorSession();
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

  it("paginates the complete operator archive with a stable cursor", async () => {
    await upsertAlias();
    for (let index = 0; index < 3; index += 1) {
      const raw = [
        "From: Sender <sender@example.com>",
        "To: hidden.one@icloud.com",
        "X-Original-To: hidden.one@icloud.com",
        `Subject: Archive page ${index}`,
        `Message-ID: <archive-page-${index}@example.com>`,
        "Content-Type: text/plain; charset=utf-8",
        "",
        `Searchable body ${index}`,
      ].join("\r\n");
      await worker.email?.(
        emailMessage(raw),
        mailboxEnv,
        {} as ExecutionContext,
      );
    }
    const operatorCookie = await createOperatorSession();
    const firstResponse = await SELF.fetch(
      "https://example.com/api/operator/messages?limit=2",
      { headers: { Cookie: operatorCookie } },
    );
    const first = (await firstResponse.json()) as {
      messages: Array<{ id: string }>;
      next_cursor: string;
      has_more: boolean;
    };
    expect(first.messages).toHaveLength(2);
    expect(first.has_more).toBe(true);
    expect(first.next_cursor).toMatch(/^[A-Za-z0-9_-]+$/);

    const secondResponse = await SELF.fetch(
      `https://example.com/api/operator/messages?limit=2&cursor=${encodeURIComponent(first.next_cursor)}`,
      { headers: { Cookie: operatorCookie } },
    );
    const second = (await secondResponse.json()) as {
      messages: Array<{ id: string }>;
      next_cursor: string;
      has_more: boolean;
    };
    expect(second.messages).toHaveLength(1);
    expect(second.has_more).toBe(false);
    expect(second.next_cursor).toBe("");
    expect(
      new Set([...first.messages, ...second.messages].map((item) => item.id))
        .size,
    ).toBe(3);

    const invalid = await SELF.fetch(
      "https://example.com/api/operator/messages?cursor=invalid!",
      { headers: { Cookie: operatorCookie } },
    );
    expect(invalid.status).toBe(422);
  });

  it("archives original HTML and encrypted attachments for operator download", async () => {
    await upsertAlias();
    const boundary = "archive-boundary";
    const raw = [
      "From: OpenAI <relay-message@icloud.com>",
      "To: hidden.one@icloud.com",
      "X-Apple-Original-Recipient: hidden.one@icloud.com",
      "Subject: Your ChatGPT archive",
      "Message-ID: <archive@example.com>",
      `Content-Type: multipart/mixed; boundary=${boundary}`,
      "",
      `--${boundary}`,
      "Content-Type: text/html; charset=utf-8",
      "",
      '<table><tr><td style="color:#c44">Original layout</td></tr></table>',
      '<img src="cid:logo@example"><img src="https://tracker.example/pixel.png">',
      "<script>alert(1)</script>",
      `--${boundary}`,
      "Content-Type: image/png",
      "Content-ID: <logo@example>",
      'Content-Disposition: inline; filename="logo.png"',
      "Content-Transfer-Encoding: base64",
      "",
      "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl6sAAAAASUVORK5CYII=",
      `--${boundary}`,
      'Content-Type: text/plain; name="report.txt"',
      'Content-Disposition: attachment; filename="report.txt"',
      "Content-Transfer-Encoding: base64",
      "",
      "YXR0YWNobWVudC1ib2R5Cg==",
      `--${boundary}--`,
      "",
    ].join("\r\n");
    await worker.email?.(
      emailMessage(raw, "relay-message@icloud.com"),
      mailboxEnv,
      {} as ExecutionContext,
    );
    await worker.email?.(
      emailMessage(raw, "relay-message@icloud.com"),
      mailboxEnv,
      {} as ExecutionContext,
    );

    const operatorCookie = await createOperatorSession();
    const listResponse = await SELF.fetch(
      "https://example.com/api/operator/messages",
      { headers: { Cookie: operatorCookie } },
    );
    const listPayload = (await listResponse.json()) as {
      messages: Array<{
        id: string;
        hasHtml: boolean;
        attachments: Array<{
          id: string;
          filename: string;
          mimeType: string;
          size: number;
        }>;
      }>;
    };
    expect(listPayload.messages).toHaveLength(1);
    const archived = listPayload.messages[0];
    expect(archived).toMatchObject({ hasHtml: true });
    expect(archived).not.toHaveProperty("html");
    expect(archived?.attachments).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          filename: "logo.png",
          mimeType: "image/png",
          inline: true,
        }),
        expect.objectContaining({
          filename: "report.txt",
          mimeType: "text/plain",
          size: 16,
          inline: false,
        }),
      ]),
    );
    const attachmentId =
      archived?.attachments.find((item) => item.filename === "report.txt")
        ?.id ?? "";

    const deniedHtml = await SELF.fetch(
      `https://example.com/api/operator/messages/${archived?.id}/html`,
    );
    expect(deniedHtml.status).toBe(401);
    const htmlResponse = await SELF.fetch(
      `https://example.com/api/operator/messages/${archived?.id}/html`,
      { headers: { Cookie: operatorCookie } },
    );
    expect(htmlResponse.status).toBe(200);
    expect(htmlResponse.headers.get("Content-Security-Policy")).toContain(
      "default-src 'none'",
    );
    expect(htmlResponse.headers.get("Content-Security-Policy")).toContain(
      "sandbox",
    );
    expect(htmlResponse.headers.get("X-Frame-Options")).toBe("SAMEORIGIN");
    const archivedHtml = await htmlResponse.text();
    expect(archivedHtml).toContain("Original layout");
    expect(archivedHtml).toContain("data:image/png;base64,");
    expect(archivedHtml).not.toContain("cid:logo@example");
    expect(archivedHtml).not.toContain("tracker.example");
    expect(archivedHtml).not.toContain("<script");

    const deniedAttachment = await SELF.fetch(
      `https://example.com/api/operator/messages/${archived?.id}/attachments/${attachmentId}`,
    );
    expect(deniedAttachment.status).toBe(401);
    const attachmentResponse = await SELF.fetch(
      `https://example.com/api/operator/messages/${archived?.id}/attachments/${attachmentId}`,
      { headers: { Cookie: operatorCookie } },
    );
    expect(attachmentResponse.status).toBe(200);
    expect(attachmentResponse.headers.get("Content-Type")).toContain(
      "text/plain",
    );
    expect(attachmentResponse.headers.get("Content-Disposition")).toContain(
      'filename="report.txt"',
    );
    expect(await attachmentResponse.text()).toBe("attachment-body\n");
    const missingAttachment = await SELF.fetch(
      `https://example.com/api/operator/messages/${archived?.id}/attachments/missing`,
      { headers: { Cookie: operatorCookie } },
    );
    expect(missingAttachment.status).toBe(404);

    const rowCount = await mailboxEnv.DB.prepare(
      "SELECT COUNT(*) AS count FROM message_attachments",
    ).first<{ count: number }>();
    expect(rowCount?.count).toBe(2);
    const storedObjects = await mailboxEnv.ATTACHMENTS.list({
      prefix: "mail/",
    });
    expect(storedObjects.keys).toHaveLength(2);
    const encryptedObject = await mailboxEnv.ATTACHMENTS.get(
      storedObjects.keys[0]?.name ?? "",
      "arrayBuffer",
    );
    expect(encryptedObject).not.toBeNull();
    expect(
      new TextDecoder().decode(encryptedObject ?? undefined),
    ).not.toContain("attachment-body");
    const storedMetadata = await mailboxEnv.DB.prepare(
      `SELECT metadata_ciphertext
         FROM message_attachments
        LIMIT 1`,
    ).first<{ metadata_ciphertext: string }>();
    expect(storedMetadata?.metadata_ciphertext).not.toContain("report.txt");

    await mailboxEnv.DB.prepare(
      "UPDATE messages SET expires_at = 1, retention_class = 'temporary'",
    ).run();
    await worker.scheduled?.(
      {} as ScheduledController,
      mailboxEnv,
      {} as ExecutionContext,
    );
    expect(
      await mailboxEnv.DB.prepare(
        "SELECT COUNT(*) AS count FROM message_attachments",
      ).first<{ count: number }>(),
    ).toMatchObject({ count: 0 });
    expect(
      (await mailboxEnv.ATTACHMENTS.list({ prefix: "mail/" })).keys,
    ).toHaveLength(0);
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
