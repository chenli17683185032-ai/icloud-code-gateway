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
  const setCookie = response.headers.get("Set-Cookie") ?? "";
  expect(setCookie).toContain("Max-Age=900");
  return setCookie.split(";", 1)[0] ?? "";
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
  const setCookie = response.headers.get("Set-Cookie") ?? "";
  expect(setCookie).toContain("Max-Age=86400");
  return setCookie.split(";", 1)[0] ?? "";
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
    expect(html).toContain("只显示验证码");
    expect(html).not.toContain('id="message-reader"');

    const script = await (
      await SELF.fetch("https://example.com/app.js")
    ).text();
    expect(script).not.toContain("localStorage");
    expect(script).not.toContain("sessionStorage");
    expect(script).not.toContain("message.body");
    expect(script).not.toContain("message.subject");
    expect(script).not.toContain("message.html");

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
    expect(adminHtml).toContain("iCloud 邮件管理");
    expect(adminHtml).toContain("邮件收件箱");
    expect(adminHtml).toContain('id="operator-search-input"');
    const adminScript = await (
      await SELF.fetch("https://example.com/admin/app.js")
    ).text();
    expect(adminScript).toContain('classList.add("operator-active")');
    expect(adminScript).toContain('classList.remove("operator-active")');
    expect(adminScript).toContain("async function loadAllMessages");
    expect(adminScript).toContain("fetchMessagePage");
    expect(adminScript).toContain("messageSearchText");
    expect(adminScript).toContain(
      'window.location.replace("/admin/operator-session")',
    );
    expect(adminScript).toContain('window.location.assign("/admin")');
    expect(adminScript).toContain("elements.logoutButton.hidden = true");

    const mergedAdminPage = await SELF.fetch("https://example.com/admin/mail/");
    expect(mergedAdminPage.status).toBe(200);
    expect(await mergedAdminPage.text()).toContain("iCloud 邮件管理");
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

  it("exposes remote key presence without exposing the key", async () => {
    await upsertAlias();
    const response = await SELF.fetch(
      "https://example.com/control/v1/aliases/status",
      {
        method: "POST",
        headers: controlHeaders,
        body: JSON.stringify({
          emails: ["hidden.one@icloud.com", "missing@icloud.com"],
        }),
      },
    );
    expect(response.status).toBe(200);
    const payload = (await response.json()) as {
      aliases: Array<Record<string, unknown>>;
    };
    expect(payload.aliases).toEqual(
      expect.arrayContaining([
        {
          email: "hidden.one@icloud.com",
          state: "active",
          has_access_key: true,
        },
        {
          email: "missing@icloud.com",
          state: "not_found",
          has_access_key: false,
        },
      ]),
    );
    expect(JSON.stringify(payload)).not.toContain(token);
  });

  it("never replaces a remote key during an unconfirmed sync", async () => {
    await upsertAlias();
    const replacement = `icg_${"r".repeat(43)}`;
    const sync = await SELF.fetch("https://example.com/control/v1/aliases", {
      method: "POST",
      headers: controlHeaders,
      body: JSON.stringify({
        id: "local-1",
        email: "hidden.one@icloud.com",
        state: "active",
        access_key: replacement,
      }),
    });
    expect(sync.status).toBe(409);

    const oldKey = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: "hidden.one@icloud.com", key: token }),
      },
    );
    expect(oldKey.status).toBe(200);
    const newKey = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: "hidden.one@icloud.com",
          key: replacement,
        }),
      },
    );
    expect(newKey.status).toBe(401);

    const rotate = await SELF.fetch(
      "https://example.com/control/v1/aliases/by-email/hidden.one%40icloud.com/key",
      {
        method: "POST",
        headers: controlHeaders,
        body: JSON.stringify({
          id: "local-1",
          access_key: replacement,
          replace_existing: true,
        }),
      },
    );
    expect(rotate.status).toBe(200);
    const rotated = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: "hidden.one@icloud.com",
          key: replacement,
        }),
      },
    );
    expect(rotated.status).toBe(200);
  });

  it("installs the first key onto an alias that does not have one", async () => {
    const created = await SELF.fetch("https://example.com/control/v1/aliases", {
      method: "POST",
      headers: controlHeaders,
      body: JSON.stringify({
        id: "keyless-1",
        email: "keyless.box@icloud.com",
        state: "active",
      }),
    });
    expect(created.status).toBe(200);
    expect(await created.json()).toMatchObject({ has_access_key: false });

    const key = `icg_${"k".repeat(43)}`;
    const issued = await SELF.fetch(
      "https://example.com/control/v1/aliases/by-email/keyless.box%40icloud.com/key",
      {
        method: "POST",
        headers: controlHeaders,
        body: JSON.stringify({
          id: "keyless-1",
          access_key: key,
          replace_existing: false,
        }),
      },
    );
    expect(issued.status).toBe(200);

    const session = await SELF.fetch("https://example.com/api/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": "198.51.100.41",
      },
      body: JSON.stringify({ email: "keyless.box@icloud.com", token: key }),
    });
    expect(session.status).toBe(200);

    const other = `icg_${"z".repeat(43)}`;
    const blocked = await SELF.fetch(
      "https://example.com/control/v1/aliases/by-email/keyless.box%40icloud.com/key",
      {
        method: "POST",
        headers: controlHeaders,
        body: JSON.stringify({
          access_key: other,
          replace_existing: false,
        }),
      },
    );
    expect(blocked.status).toBe(409);

    const stillOriginal = await SELF.fetch("https://example.com/api/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": "198.51.100.42",
      },
      body: JSON.stringify({ email: "keyless.box@icloud.com", token: key }),
    });
    expect(stillOriginal.status).toBe(200);
  });

  it("serves the stateless standard verification-code API", async () => {
    await upsertAlias();
    const waiting = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "CF-Connecting-IP": "198.51.100.30",
        },
        body: JSON.stringify({ email: "hidden.one@icloud.com", key: token }),
      },
    );
    expect(waiting.status).toBe(200);
    await expect(waiting.json()).resolves.toMatchObject({
      status: "waiting",
      data: {
        email: "hidden.one@icloud.com",
        code: null,
        received_at: null,
        expires_at: null,
      },
      retry_after: 5,
    });

    const raw = [
      "From: OpenAI <relay-message@icloud.com>",
      "To: hidden.one@icloud.com",
      "X-Apple-Original-Recipient: hidden.one@icloud.com",
      "Subject: Your ChatGPT verification code",
      "Message-ID: <standard-api@example.com>",
      "Content-Type: text/plain; charset=utf-8",
      "",
      "Your OpenAI verification code is 987654.",
    ].join("\r\n");
    await worker.email?.(
      emailMessage(raw, "relay-message@icloud.com"),
      mailboxEnv,
      {} as ExecutionContext,
    );

    const post = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "CF-Connecting-IP": "198.51.100.31",
        },
        body: JSON.stringify({ email: "hidden.one@icloud.com", key: token }),
      },
    );
    expect(post.status).toBe(200);
    await expect(post.json()).resolves.toMatchObject({
      status: "found",
      data: {
        email: "hidden.one@icloud.com",
        code: "987654",
      },
      retry_after: null,
    });

    const get = await SELF.fetch(
      "https://example.com/api/v1/mailboxes/hidden.one%40icloud.com/verification-code",
      {
        headers: {
          Authorization: `Bearer ${token}`,
          "CF-Connecting-IP": "198.51.100.32",
        },
      },
    );
    expect(get.status).toBe(200);
    await expect(get.json()).resolves.toMatchObject({
      status: "found",
      data: { code: "987654" },
    });

    const denied = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: "hidden.one@icloud.com",
          key: `icg_${"x".repeat(43)}`,
        }),
      },
    );
    expect(denied.status).toBe(401);
    await expect(denied.json()).resolves.toMatchObject({
      status: "unauthorized",
      data: null,
    });
  });

  it("accepts the shared mailbox API password without replacing decode tokens", async () => {
    await upsertAlias();
    const keyless = await SELF.fetch("https://example.com/control/v1/aliases", {
      method: "POST",
      headers: controlHeaders,
      body: JSON.stringify({
        id: "local-2",
        email: "kindly.vastest9l@icloud.com",
        label: "keyless",
        state: "active",
      }),
    });
    expect(keyless.status).toBe(200);
    expect(await keyless.json()).toMatchObject({ has_access_key: false });

    const shared = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: "hidden.one@icloud.com",
          key: "shared-api-password",
        }),
      },
    );
    expect(shared.status).toBe(200);
    await expect(shared.json()).resolves.toMatchObject({
      status: "waiting",
      data: { email: "hidden.one@icloud.com", code: null },
    });

    const keylessShared = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: "kindly.vastest9l@icloud.com",
          key: "shared-api-password",
        }),
      },
    );
    expect(keylessShared.status).toBe(200);

    const decodeToken = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: "hidden.one@icloud.com",
          key: token,
        }),
      },
    );
    expect(decodeToken.status).toBe(200);

    const sessionDenied = await SELF.fetch("https://example.com/api/session", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "CF-Connecting-IP": "198.51.100.40",
      },
      body: JSON.stringify({
        email: "hidden.one@icloud.com",
        token: "shared-api-password",
      }),
    });
    expect(sessionDenied.status).toBe(422);
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

  it("shows users only the verification code when mail also carries documents and file changes", async () => {
    await upsertAlias();
    const boundary = "user-boundary";
    const guide = "SETUP-GUIDE-DO-NOT-LEAK";
    const diff =
      "diff --git a/secrets/config.txt\n+changed-file-contents-DO-NOT-LEAK";
    const attachmentName = "setup-guide.txt";
    const attachmentBody = "guide-file-bytes-DO-NOT-LEAK";
    const verification = [
      "From: OpenAI <relay-message@icloud.com>",
      "To: hidden.one@icloud.com",
      "X-Apple-Original-Recipient: hidden.one@icloud.com",
      "Subject: Your ChatGPT verification code",
      "Message-ID: <code-with-guide@example.com>",
      `Content-Type: multipart/mixed; boundary=${boundary}`,
      "",
      `--${boundary}`,
      "Content-Type: text/plain; charset=utf-8",
      "",
      "Your OpenAI verification code is 246810.",
      guide,
      diff,
      `--${boundary}`,
      `Content-Type: text/plain; name="${attachmentName}"`,
      `Content-Disposition: attachment; filename="${attachmentName}"`,
      "Content-Transfer-Encoding: base64",
      "",
      btoa(`${attachmentBody}\n`),
      `--${boundary}--`,
      "",
    ].join("\r\n");
    const instructions = [
      "From: Docs <docs@example.com>",
      "To: hidden.one@icloud.com",
      "X-Original-To: hidden.one@icloud.com",
      "Subject: 说明文件已更新",
      "Message-ID: <instructions@example.com>",
      "Content-Type: text/plain; charset=utf-8",
      "",
      "完整说明文档。verification code 135790。",
      "文件变更：changed-file-contents-DO-NOT-LEAK",
    ].join("\r\n");
    await worker.email?.(
      emailMessage(verification, "relay-message@icloud.com"),
      mailboxEnv,
      {} as ExecutionContext,
    );
    await worker.email?.(
      emailMessage(instructions),
      mailboxEnv,
      {} as ExecutionContext,
    );

    const userCookie = await createSession();
    const userResponse = await SELF.fetch("https://example.com/api/messages", {
      headers: { Cookie: userCookie },
    });
    const userText = await userResponse.text();
    const userPayload = JSON.parse(userText) as {
      messages: Array<Record<string, unknown>>;
    };
    expect(userPayload.messages).toHaveLength(1);
    expect(Object.keys(userPayload.messages[0] ?? {}).sort()).toEqual([
      "code",
      "expiresAt",
      "id",
      "receivedAt",
    ]);
    expect(userPayload.messages[0]).toMatchObject({ code: "246810" });
    for (const secret of [
      guide,
      diff,
      attachmentName,
      attachmentBody,
      "135790",
    ]) {
      expect(userText).not.toContain(secret);
    }

    const direct = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: "hidden.one@icloud.com", key: token }),
      },
    );
    const directText = await direct.text();
    expect(directText).toContain("246810");
    for (const secret of [
      guide,
      diff,
      attachmentName,
      attachmentBody,
      "135790",
    ]) {
      expect(directText).not.toContain(secret);
    }

    const operatorCookie = await createOperatorSession();
    const operatorResponse = await SELF.fetch(
      "https://example.com/api/operator/messages",
      { headers: { Cookie: operatorCookie } },
    );
    const operatorPayload = (await operatorResponse.json()) as {
      messages: Array<{
        id: string;
        subject: string;
        body: string;
        attachments: Array<{ id: string; filename: string }>;
      }>;
    };
    const archived = operatorPayload.messages.find((item) =>
      item.body.includes(guide),
    );
    expect(archived?.body).toContain(diff);
    expect(archived?.attachments).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ filename: attachmentName }),
      ]),
    );
    const attachmentId = archived?.attachments[0]?.id ?? "";
    for (const path of ["html", `attachments/${attachmentId}`]) {
      const denied = await SELF.fetch(
        `https://example.com/api/operator/messages/${archived?.id}/${path}`,
        { headers: { Cookie: userCookie } },
      );
      expect(denied.status).toBe(401);
    }
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

  it("paginates legacy permanent mail past its old expiry without exposing expired codes", async () => {
    await upsertAlias();
    for (let index = 0; index < 3; index += 1) {
      const raw = [
        "From: OpenAI <noreply@tm.openai.com>",
        "To: hidden.one@icloud.com",
        "X-Original-To: hidden.one@icloud.com",
        `Subject: Archive page ${index}`,
        `Message-ID: <archive-page-${index}@example.com>`,
        "Content-Type: text/plain; charset=utf-8",
        "",
        `Your OpenAI verification code is 12345${index}.`,
      ].join("\r\n");
      await worker.email?.(
        emailMessage(raw),
        mailboxEnv,
        {} as ExecutionContext,
      );
    }
    // Reproduce legacy retention migration: the archive flag was updated,
    // but the previous temporary expiry was left unchanged.
    await mailboxEnv.DB.prepare(
      "UPDATE messages SET expires_at = 1, retention_class = 'permanent'",
    ).run();
    const temporaryMail = [
      "From: OpenAI <noreply@tm.openai.com>",
      "To: hidden.one@icloud.com",
      "Subject: Your OpenAI verification code",
      "Message-ID: <expired-temporary@example.com>",
      "Content-Type: text/plain; charset=utf-8",
      "",
      "Your OpenAI verification code is 987654.",
    ].join("\r\n");
    await worker.email?.(
      emailMessage(temporaryMail),
      mailboxEnv,
      {} as ExecutionContext,
    );
    await mailboxEnv.DB.prepare(
      "UPDATE messages SET expires_at = 1 WHERE retention_class = 'temporary'",
    ).run();

    const operatorCookie = await createOperatorSession();
    const firstResponse = await SELF.fetch(
      "https://example.com/api/operator/messages?limit=2",
      { headers: { Cookie: operatorCookie } },
    );
    const first = (await firstResponse.json()) as {
      messages: Array<{ id: string; permanent: boolean; subject: string }>;
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
      messages: Array<{ id: string; permanent: boolean; subject: string }>;
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
    expect(
      [...first.messages, ...second.messages].every(
        (item) => item.permanent && item.subject.startsWith("Archive page"),
      ),
    ).toBe(true);

    const userCookie = await createSession();
    const userMessages = await SELF.fetch("https://example.com/api/messages", {
      headers: { Cookie: userCookie },
    });
    await expect(userMessages.json()).resolves.toMatchObject({ messages: [] });
    const publicCode = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: "hidden.one@icloud.com", key: token }),
      },
    );
    await expect(publicCode.json()).resolves.toMatchObject({
      status: "waiting",
      data: { code: null, received_at: null, expires_at: null },
    });
    for (const cookie of ["", userCookie]) {
      const denied = await SELF.fetch(
        "https://example.com/api/operator/messages",
        { headers: cookie ? { Cookie: cookie } : {} },
      );
      expect(denied.status).toBe(401);
    }

    const invalid = await SELF.fetch(
      "https://example.com/api/operator/messages?cursor=invalid!",
      { headers: { Cookie: operatorCookie } },
    );
    expect(invalid.status).toBe(422);
  });

  it.each([false, true])(
    "archives original HTML and encrypted attachments for operator download (legacy expiry: %s)",
    async (legacyExpiry) => {
      await upsertAlias();
      const boundary = "archive-boundary";
      const raw = [
        "From: OpenAI <relay-message@icloud.com>",
        "To: hidden.one@icloud.com",
        "X-Apple-Original-Recipient: hidden.one@icloud.com",
        "Subject: Your ChatGPT subscription archive",
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

      if (legacyExpiry) {
        await mailboxEnv.DB.prepare(
          "UPDATE messages SET expires_at = 1 WHERE retention_class = 'permanent'",
        ).run();
      }
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
          permanent: boolean;
          expiresAt: string;
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
      expect(archived).toMatchObject({ hasHtml: true, permanent: true });
      if (legacyExpiry) {
        expect(archived?.expiresAt).toBe("1970-01-01T00:00:01.000Z");
      }
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
      const userCookie = await createSession();
      const deniedUserHtml = await SELF.fetch(
        `https://example.com/api/operator/messages/${archived?.id}/html`,
        { headers: { Cookie: userCookie } },
      );
      expect(deniedUserHtml.status).toBe(401);
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
      const deniedUserAttachment = await SELF.fetch(
        `https://example.com/api/operator/messages/${archived?.id}/attachments/${attachmentId}`,
        { headers: { Cookie: userCookie } },
      );
      expect(deniedUserAttachment.status).toBe(401);
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
      const expiredList = await SELF.fetch(
        "https://example.com/api/operator/messages",
        { headers: { Cookie: operatorCookie } },
      );
      await expect(expiredList.json()).resolves.toMatchObject({ messages: [] });
      for (const path of ["html", `attachments/${attachmentId}`]) {
        const expiredResource = await SELF.fetch(
          `https://example.com/api/operator/messages/${archived?.id}/${path}`,
          { headers: { Cookie: operatorCookie } },
        );
        expect(expiredResource.status).toBe(404);
      }
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
    },
  );

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

  it("stores an Apple IMAP message through the control plane", async () => {
    await upsertAlias();
    const raw = [
      "From: OpenAI <relay-message@icloud.com>",
      "To: hidden.one@icloud.com",
      "X-Apple-Original-Recipient: hidden.one@icloud.com",
      "Subject: Your ChatGPT verification code",
      "Message-ID: <imap-bridge@example.com>",
      "Content-Type: text/plain; charset=utf-8",
      "",
      "Your OpenAI verification code is 246810.",
    ].join("\r\n");
    const denied = await SELF.fetch("https://example.com/control/v1/messages", {
      method: "POST",
      headers: { "Content-Type": "message/rfc822" },
      body: raw,
    });
    expect(denied.status).toBe(401);

    const ingested = await SELF.fetch(
      "https://example.com/control/v1/messages",
      {
        method: "POST",
        headers: {
          Authorization: controlHeaders.Authorization,
          "Content-Type": "message/rfc822",
          "X-Envelope-From": "relay-message@icloud.com",
        },
        body: raw,
      },
    );
    expect(ingested.status).toBe(200);
    await expect(ingested.json()).resolves.toMatchObject({
      status: "ok",
      stored: 1,
      matched: 1,
      duplicate: 0,
    });

    const found = await SELF.fetch(
      "https://example.com/api/v1/verification-codes",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "CF-Connecting-IP": "198.51.100.44",
        },
        body: JSON.stringify({ email: "hidden.one@icloud.com", key: token }),
      },
    );
    expect(found.status).toBe(200);
    await expect(found.json()).resolves.toMatchObject({
      status: "found",
      data: { code: "246810" },
    });
  });
});
