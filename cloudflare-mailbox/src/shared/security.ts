import { UnauthorizedError, ValidationError } from "./errors";
import type { RuntimeConfig, SessionPayload } from "./types";

const encoder = new TextEncoder();
const decoder = new TextDecoder();
const ACCESS_TOKEN_PATTERN = /^icg_[A-Za-z0-9_-]{43}$/;
export const SESSION_COOKIE = "__Host-icg_mailbox";

function bytesToBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  const binary = atob(padded);
  const result = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    result[index] = binary.charCodeAt(index);
  }
  return result;
}

function secretBytes(value: string): Uint8Array<ArrayBuffer> {
  return base64UrlToBytes(value.trim());
}

async function hmacKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey(
    "raw",
    secretBytes(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

async function aesKey(secret: string): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", secretBytes(secret), "AES-GCM", false, [
    "encrypt",
    "decrypt",
  ]);
}

export async function hmacDigest(
  secret: string,
  value: string,
): Promise<string> {
  const signature = await crypto.subtle.sign(
    "HMAC",
    await hmacKey(secret),
    encoder.encode(value),
  );
  return bytesToBase64Url(new Uint8Array(signature));
}

export async function safeEqual(left: string, right: string): Promise<boolean> {
  const [leftHash, rightHash] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(left)),
    crypto.subtle.digest("SHA-256", encoder.encode(right)),
  ]);
  const leftBytes = new Uint8Array(leftHash);
  const rightBytes = new Uint8Array(rightHash);
  let difference = leftBytes.length ^ rightBytes.length;
  for (
    let index = 0;
    index < Math.max(leftBytes.length, rightBytes.length);
    index += 1
  ) {
    difference |= (leftBytes[index] ?? 0) ^ (rightBytes[index] ?? 0);
  }
  return difference === 0;
}

export function normalizeEmail(value: string): string {
  const email = String(value ?? "")
    .trim()
    .toLowerCase();
  const parts = email.split("@");
  if (
    email.length < 3 ||
    email.length > 254 ||
    parts.length !== 2 ||
    !parts[0] ||
    !parts[1]?.includes(".") ||
    /[\s\r\n\0]/.test(email)
  ) {
    throw new ValidationError("请输入完整邮箱地址。");
  }
  return email;
}

export function validateAccessToken(value: string): string {
  const token = String(value ?? "").trim();
  if (!ACCESS_TOKEN_PATTERN.test(token)) {
    throw new UnauthorizedError();
  }
  return token;
}

export async function aliasDigest(
  config: RuntimeConfig,
  email: string,
): Promise<string> {
  return hmacDigest(config.lookupHmacKey, `alias\0${normalizeEmail(email)}`);
}

export async function accessTokenDigest(
  config: RuntimeConfig,
  digest: string,
  token: string,
): Promise<string> {
  return hmacDigest(
    config.lookupHmacKey,
    `token\0${digest}\0${validateAccessToken(token)}`,
  );
}

export async function accessTokenLookupDigest(
  config: RuntimeConfig,
  token: string,
): Promise<string> {
  return hmacDigest(
    config.lookupHmacKey,
    `token-lookup\0${validateAccessToken(token)}`,
  );
}

export interface EncryptedValue {
  ciphertext: string;
  iv: string;
}

export async function encryptJson(
  config: RuntimeConfig,
  value: unknown,
  context: string,
): Promise<EncryptedValue> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: encoder.encode(context) },
    await aesKey(config.dataEncryptionKey),
    encoder.encode(JSON.stringify(value)),
  );
  return {
    ciphertext: bytesToBase64Url(new Uint8Array(ciphertext)),
    iv: bytesToBase64Url(iv),
  };
}

export async function decryptJson<T>(
  config: RuntimeConfig,
  encrypted: EncryptedValue,
  context: string,
): Promise<T> {
  const plaintext = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: base64UrlToBytes(encrypted.iv),
      additionalData: encoder.encode(context),
    },
    await aesKey(config.dataEncryptionKey),
    base64UrlToBytes(encrypted.ciphertext),
  );
  return JSON.parse(decoder.decode(plaintext)) as T;
}

export async function issueSession(
  config: RuntimeConfig,
  digest: string,
  tokenDigest: string,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<{ token: string; expiresAt: number }> {
  const payload: SessionPayload = {
    version: 1,
    aliasDigest: digest,
    tokenDigest,
    expiresAt: nowSeconds + config.sessionTtlSeconds,
    nonce: bytesToBase64Url(crypto.getRandomValues(new Uint8Array(12))),
  };
  const encoded = bytesToBase64Url(encoder.encode(JSON.stringify(payload)));
  const signature = await hmacDigest(config.sessionSigningKey, encoded);
  return { token: `${encoded}.${signature}`, expiresAt: payload.expiresAt };
}

export async function verifySession(
  config: RuntimeConfig,
  token: string,
  nowSeconds = Math.floor(Date.now() / 1000),
): Promise<SessionPayload> {
  const [encoded, suppliedSignature, extra] = String(token ?? "").split(".");
  if (!encoded || !suppliedSignature || extra)
    throw new UnauthorizedError("会话已失效，请重新查询。");
  const expectedSignature = await hmacDigest(config.sessionSigningKey, encoded);
  if (!(await safeEqual(expectedSignature, suppliedSignature))) {
    throw new UnauthorizedError("会话已失效，请重新查询。");
  }
  try {
    const payload = JSON.parse(
      decoder.decode(base64UrlToBytes(encoded)),
    ) as SessionPayload;
    if (
      payload.version !== 1 ||
      !payload.aliasDigest ||
      !payload.tokenDigest ||
      payload.expiresAt <= nowSeconds
    ) {
      throw new Error("expired");
    }
    return payload;
  } catch {
    throw new UnauthorizedError("会话已失效，请重新查询。");
  }
}

export function readCookie(request: Request, name: string): string {
  const header = request.headers.get("Cookie") ?? "";
  for (const part of header.split(";")) {
    const [key, ...value] = part.trim().split("=");
    if (key === name) return value.join("=");
  }
  return "";
}

export function sessionCookie(token: string, maxAge: number): string {
  return `${SESSION_COOKIE}=${token}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Strict`;
}

export function clearSessionCookie(): string {
  return `${SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict`;
}
