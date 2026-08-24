import { describe, expect, it } from "vitest";
import { extractVerificationCode } from "../src/mail/extract-code";
import { htmlToText, parseIncomingEmail } from "../src/mail/parse";

describe("mail parsing", () => {
  it("extracts recipients, body and a nearby six-digit code", async () => {
    const raw = [
      "From: OpenAI <noreply@tm.openai.com>",
      "To: Hidden.One@icloud.com",
      "X-Original-To: hidden.one@icloud.com",
      "Subject: Your ChatGPT verification code",
      "Message-ID: <one@example.com>",
      "Content-Type: text/plain; charset=utf-8",
      "",
      "Your verification code is 123456. Order 999999 is not the code.",
    ].join("\r\n");
    const parsed = await parseIncomingEmail(raw, 50_000);
    expect(parsed.recipients).toContain("hidden.one@icloud.com");
    expect(parsed.body).toContain("123456");
    expect(
      extractVerificationCode(parsed.sender, parsed.subject, parsed.body),
    ).toBe("123456");
  });

  it("supports Grok alphanumeric codes", () => {
    expect(
      extractVerificationCode(
        "Grok <noreply@x.ai>",
        "Your Grok security code",
        "Use this code to continue signing in: A1B-2C3",
      ),
    ).toBe("A1B-2C3");
  });

  it("converts HTML to inert plain text", () => {
    expect(
      htmlToText(
        "<style>.x{}</style><p>Hello &amp; 你好</p><script>alert(1)</script>",
      ),
    ).toBe("Hello & 你好");
  });
});
