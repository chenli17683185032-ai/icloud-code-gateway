import { describe, expect, it } from "vitest";
import { loadConfig } from "../src/shared/config";
import {
  accessTokenDigest,
  accessTokenLookupDigest,
  aliasDigest,
  decryptJson,
  encryptJson,
  issueSession,
  normalizeEmail,
  verifySession,
} from "../src/shared/security";
import type { Env } from "../src/shared/types";

const env = {
  CONTROL_PLANE_TOKEN: "control-token-abcdefghijklmnop",
  LOOKUP_HMAC_KEY: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
  DATA_ENCRYPTION_KEY: "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE=",
  SESSION_SIGNING_KEY: "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI=",
  OPERATOR_ACCESS_TOKEN: `icg_${"o".repeat(43)}`,
} as Env;
const config = loadConfig(env);
const token = `icg_${"a".repeat(43)}`;

describe("security primitives", () => {
  it("normalizes aliases and binds token digests to one alias", async () => {
    expect(normalizeEmail(" Hidden.One@iCloud.com ")).toBe(
      "hidden.one@icloud.com",
    );
    const first = await aliasDigest(config, "hidden.one@icloud.com");
    const second = await aliasDigest(config, "hidden.two@icloud.com");
    expect(first).not.toBe(second);
    expect(await accessTokenDigest(config, first, token)).not.toBe(
      await accessTokenDigest(config, second, token),
    );
    expect(await accessTokenLookupDigest(config, token)).toHaveLength(43);
  });

  it("encrypts payloads with authenticated context", async () => {
    const encrypted = await encryptJson(
      config,
      { body: "验证码 123456" },
      "message:one",
    );
    await expect(
      decryptJson(config, encrypted, "message:one"),
    ).resolves.toEqual({
      body: "验证码 123456",
    });
    await expect(
      decryptJson(config, encrypted, "message:two"),
    ).rejects.toBeTruthy();
  });

  it("issues expiring sessions", async () => {
    const session = await issueSession(
      config,
      "alias-digest",
      "token-digest",
      "alias",
      1000,
    );
    await expect(
      verifySession(config, session.token, 1001),
    ).resolves.toMatchObject({
      aliasDigest: "alias-digest",
      tokenDigest: "token-digest",
    });
    await expect(
      verifySession(config, session.token, session.expiresAt),
    ).rejects.toThrow();
  });
});
