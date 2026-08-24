import { AppError } from "./errors";
import type { Env, RuntimeConfig } from "./types";

function decodeBase64Secret(value: string): Uint8Array {
  const normalized = value.trim().replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  try {
    return Uint8Array.from(atob(padded), (character) =>
      character.charCodeAt(0),
    );
  } catch {
    throw new AppError("configuration_error", 500, "Worker secret is invalid.");
  }
}

function required(
  value: string | undefined,
  name: string,
  minimum = 1,
): string {
  const resolved = String(value ?? "").trim();
  if (resolved.length < minimum) {
    throw new AppError(
      "configuration_error",
      500,
      `${name} is not configured.`,
    );
  }
  return resolved;
}

function secret32(value: string | undefined, name: string): string {
  const resolved = required(value, name);
  if (decodeBase64Secret(resolved).byteLength !== 32) {
    throw new AppError(
      "configuration_error",
      500,
      `${name} must decode to 32 bytes.`,
    );
  }
  return resolved;
}

function boundedInteger(
  value: string | undefined,
  fallback: number,
  minimum: number,
  maximum: number,
  name: string,
): number {
  const parsed = Number.parseInt(String(value ?? fallback), 10);
  if (!Number.isInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new AppError("configuration_error", 500, `${name} is invalid.`);
  }
  return parsed;
}

export function loadConfig(env: Env): RuntimeConfig {
  const inboxAddress = String(env.INBOX_ADDRESS ?? "")
    .trim()
    .toLowerCase();
  if (
    inboxAddress &&
    (inboxAddress.split("@").length !== 2 || /[\s\r\n\0]/.test(inboxAddress))
  ) {
    throw new AppError("configuration_error", 500, "INBOX_ADDRESS is invalid.");
  }
  return {
    controlPlaneToken: required(
      env.CONTROL_PLANE_TOKEN,
      "CONTROL_PLANE_TOKEN",
      24,
    ),
    lookupHmacKey: secret32(env.LOOKUP_HMAC_KEY, "LOOKUP_HMAC_KEY"),
    dataEncryptionKey: secret32(env.DATA_ENCRYPTION_KEY, "DATA_ENCRYPTION_KEY"),
    sessionSigningKey: secret32(env.SESSION_SIGNING_KEY, "SESSION_SIGNING_KEY"),
    emailRetentionSeconds: boundedInteger(
      env.EMAIL_RETENTION_SECONDS,
      24 * 60 * 60,
      5 * 60,
      30 * 24 * 60 * 60,
      "EMAIL_RETENTION_SECONDS",
    ),
    sessionTtlSeconds: boundedInteger(
      env.SESSION_TTL_SECONDS,
      15 * 60,
      60,
      24 * 60 * 60,
      "SESSION_TTL_SECONDS",
    ),
    messageQueryLimit: boundedInteger(
      env.MESSAGE_QUERY_LIMIT,
      20,
      1,
      50,
      "MESSAGE_QUERY_LIMIT",
    ),
    maxBodyChars: boundedInteger(
      env.MAX_BODY_CHARS,
      50_000,
      1_000,
      200_000,
      "MAX_BODY_CHARS",
    ),
    maxEmailBytes: boundedInteger(
      env.MAX_EMAIL_BYTES,
      5_000_000,
      10_000,
      25_000_000,
      "MAX_EMAIL_BYTES",
    ),
    authAttemptsPerMinute: boundedInteger(
      env.AUTH_ATTEMPTS_PER_MINUTE,
      10,
      1,
      120,
      "AUTH_ATTEMPTS_PER_MINUTE",
    ),
    inboxAddress,
  };
}
