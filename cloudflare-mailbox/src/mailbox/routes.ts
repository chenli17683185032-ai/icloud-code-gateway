import { Hono } from "hono";
import { mailboxSessionSchema } from "../aliases/schema";
import { RateLimitError, UnauthorizedError } from "../shared/errors";
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
