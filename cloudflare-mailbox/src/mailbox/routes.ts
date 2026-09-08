import { Hono } from "hono";
import type { Context } from "hono";
import {
  mailboxSessionSchema,
  verificationCodeRequestSchema,
} from "../aliases/schema";
import {
  RateLimitError,
  UnauthorizedError,
  ValidationError,
} from "../shared/errors";
import { clientIp, noStore, parseJson } from "../shared/http";
import {
  clearSessionCookie,
  hmacDigest,
  issueSession,
  readCookie,
  SESSION_COOKIE,
  sessionCookie,
  verifySession,
} from "../shared/security";
import { services } from "../shared/services";
import type { WorkerContext } from "../shared/types";

export const mailboxRoutes = new Hono<WorkerContext>();

function apiError(
  context: Context<WorkerContext>,
  status: "unauthorized" | "invalid_request",
  httpStatus: 401 | 422,
  headers?: Record<string, string>,
) {
  return context.json(
    {
      status,
      data: null,
      retry_after: null,
      request_id: context.get("requestId"),
    },
    httpStatus,
    headers,
  );
}

function pathEmail(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    throw new ValidationError("邮箱地址不正确。");
  }
}

function bearerKey(context: Context<WorkerContext>): string {
  const authorization = context.req.header("Authorization") ?? "";
  if (!authorization.toLowerCase().startsWith("bearer ")) {
    throw new UnauthorizedError("请使用 Authorization: Bearer <key> 认证。");
  }
  const key = authorization.slice(7).trim();
  if (!key)
    throw new UnauthorizedError("请使用 Authorization: Bearer <key> 认证。");
  return key;
}

async function directVerificationCode(
  context: Context<WorkerContext>,
  email: string,
  key: string,
) {
  const { config, aliases, messages, rateLimits } = services(context.env);
  const now = Math.floor(Date.now() / 1000);
  const fingerprint = await hmacDigest(
    config.lookupHmacKey,
    `api-code\0${clientIp(context.req.raw)}`,
  );
  if (
    (await rateLimits.consume(fingerprint, now)) > config.apiRequestsPerMinute
  ) {
    return context.json(
      {
        status: "rate_limited",
        data: null,
        retry_after: 60,
        request_id: context.get("requestId"),
      },
      429,
      { "Retry-After": "60" },
    );
  }
  let authenticated;
  try {
    authenticated = await aliases.authenticate(email, key);
  } catch (error) {
    if (error instanceof UnauthorizedError) {
      return apiError(context, "unauthorized", 401, {
        "WWW-Authenticate": "Bearer",
      });
    }
    throw error;
  }
  const [latest] = await messages.listCodes(
    authenticated.row.alias_digest,
    1,
    now,
  );
  const data = {
    email: authenticated.secret.email,
    code: latest?.code ?? null,
    received_at: latest?.receivedAt ?? null,
    expires_at: latest?.expiresAt ?? null,
  };
  return context.json({
    status: latest ? "found" : "waiting",
    data,
    retry_after: latest ? null : 5,
    request_id: context.get("requestId"),
  });
}

mailboxRoutes.use("*", async (context, next) => {
  noStore(context);
  await next();
});

mailboxRoutes.post("/session", async (context) => {
  const { config, aliases, rateLimits } = services(context.env);
  const now = Math.floor(Date.now() / 1000);
  const fingerprint = await hmacDigest(
    config.lookupHmacKey,
    `ip\0${clientIp(context.req.raw)}`,
  );
  if (
    (await rateLimits.consume(fingerprint, now)) > config.authAttemptsPerMinute
  ) {
    throw new RateLimitError();
  }
  const input = await parseJson(context, mailboxSessionSchema);
  const authenticated = input.email
    ? await aliases.authenticate(input.email, input.token)
    : await aliases.authenticateToken(input.token);
  if (!authenticated.row.token_digest) throw new UnauthorizedError();
  const session = await issueSession(
    config,
    authenticated.row.alias_digest,
    authenticated.row.token_digest,
    "alias",
    now,
  );
  context.header(
    "Set-Cookie",
    sessionCookie(session.token, config.sessionTtlSeconds),
  );
  return context.json({
    status: "ok",
    mailbox: { email: authenticated.secret.email, mode: "code_only" },
    expires_at: new Date(session.expiresAt * 1000).toISOString(),
  });
});

// Standard machine-to-machine API. Unlike /session this does not create a
// cookie, making it safe for server-side integrations and cron jobs.
mailboxRoutes.post("/v1/verification-codes", async (context) => {
  try {
    const input = await parseJson(context, verificationCodeRequestSchema);
    return await directVerificationCode(context, input.email, input.key);
  } catch (error) {
    if (error instanceof ValidationError) {
      return apiError(context, "invalid_request", 422);
    }
    throw error;
  }
});

mailboxRoutes.get("/v1/mailboxes/:email/verification-code", async (context) => {
  try {
    return await directVerificationCode(
      context,
      pathEmail(context.req.param("email")),
      bearerKey(context),
    );
  } catch (error) {
    if (error instanceof UnauthorizedError) {
      return apiError(context, "unauthorized", 401, {
        "WWW-Authenticate": "Bearer",
      });
    }
    if (error instanceof ValidationError) {
      return apiError(context, "invalid_request", 422);
    }
    throw error;
  }
});

mailboxRoutes.get("/session", async (context) => {
  const { config, aliases } = services(context.env);
  const sessionToken = readCookie(context.req.raw, SESSION_COOKIE);
  if (!sessionToken)
    return context.json({ status: "ok", authenticated: false });
  try {
    const session = await verifySession(config, sessionToken);
    if (session.access !== "alias") throw new UnauthorizedError();
    const authenticated = await aliases.authenticateSession(session);
    return context.json({
      status: "ok",
      authenticated: true,
      mailbox: { email: authenticated.secret.email, mode: "code_only" },
      expires_at: new Date(session.expiresAt * 1000).toISOString(),
    });
  } catch (error) {
    if (!(error instanceof UnauthorizedError)) throw error;
    context.header("Set-Cookie", clearSessionCookie());
    return context.json({ status: "ok", authenticated: false });
  }
});

mailboxRoutes.get("/messages", async (context) => {
  const { config, aliases, messages } = services(context.env);
  const sessionToken = readCookie(context.req.raw, SESSION_COOKIE);
  if (!sessionToken) throw new UnauthorizedError("请先输入邮箱和 Token。");
  const session = await verifySession(config, sessionToken);
  if (session.access !== "alias") throw new UnauthorizedError();
  const authenticated = await aliases.authenticateSession(session);
  const requestedLimit = Number.parseInt(context.req.query("limit") ?? "", 10);
  const limit = Number.isFinite(requestedLimit)
    ? Math.min(Math.max(requestedLimit, 1), config.messageQueryLimit)
    : config.messageQueryLimit;
  return context.json({
    status: "ok",
    mailbox: {
      email: authenticated.secret.email,
      mode: "code_only",
      retention_seconds: config.emailRetentionSeconds,
    },
    messages: await messages.listCodes(authenticated.row.alias_digest, limit),
  });
});

mailboxRoutes.post("/logout", (context) => {
  context.header("Set-Cookie", clearSessionCookie());
  return context.json({ status: "ok" });
});
