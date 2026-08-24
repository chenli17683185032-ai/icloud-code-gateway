import { Hono } from "hono";
import { operatorSessionSchema } from "../aliases/schema";
import { RateLimitError, UnauthorizedError } from "../shared/errors";
import { clientIp, noStore, parseJson } from "../shared/http";
import {
  accessTokenLookupDigest,
  clearSessionCookie,
  hmacDigest,
  issueSession,
  readCookie,
  safeEqual,
  SESSION_COOKIE,
  sessionCookie,
  verifySession,
} from "../shared/security";
import { services } from "../shared/services";
import type { RuntimeConfig, WorkerContext } from "../shared/types";

export const operatorRoutes = new Hono<WorkerContext>();

operatorRoutes.use("*", async (context, next) => {
  noStore(context);
  await next();
});

async function requireOperatorSession(
  config: RuntimeConfig,
  request: Request,
): Promise<void> {
  const token = readCookie(request, SESSION_COOKIE);
  if (!token) throw new UnauthorizedError("请先登录操作员后台。");
  const session = await verifySession(config, token);
  const expected = await accessTokenLookupDigest(
    config,
    config.operatorAccessToken,
  );
  if (
    session.access !== "operator" ||
    !(await safeEqual(session.tokenDigest, expected))
  ) {
    throw new UnauthorizedError("操作员会话已失效。");
  }
}

operatorRoutes.post("/session", async (context) => {
  const { config, rateLimits } = services(context.env);
  const now = Math.floor(Date.now() / 1000);
  const fingerprint = await hmacDigest(
    config.lookupHmacKey,
    `operator-ip\0${clientIp(context.req.raw)}`,
  );
  if (
    (await rateLimits.consume(fingerprint, now)) > config.authAttemptsPerMinute
  ) {
    throw new RateLimitError();
  }
  const input = await parseJson(context, operatorSessionSchema);
  if (!(await safeEqual(config.operatorAccessToken, input.token))) {
    throw new UnauthorizedError("操作员 Token 不正确。");
  }
  const tokenDigest = await accessTokenLookupDigest(config, input.token);
  const session = await issueSession(config, "", tokenDigest, "operator", now);
  context.header(
    "Set-Cookie",
    sessionCookie(session.token, config.sessionTtlSeconds),
  );
  return context.json({
    status: "ok",
    mode: "operator",
    expires_at: new Date(session.expiresAt * 1000).toISOString(),
  });
});

operatorRoutes.get("/session", async (context) => {
  const { config } = services(context.env);
  try {
    await requireOperatorSession(config, context.req.raw);
    return context.json({
      status: "ok",
      authenticated: true,
      mode: "operator",
    });
  } catch (error) {
    if (!(error instanceof UnauthorizedError)) throw error;
    context.header("Set-Cookie", clearSessionCookie());
    return context.json({ status: "ok", authenticated: false });
  }
});

operatorRoutes.get("/messages", async (context) => {
  const { config, messages } = services(context.env);
  await requireOperatorSession(config, context.req.raw);
  const requested = Number.parseInt(context.req.query("limit") ?? "", 10);
  const limit = Number.isFinite(requested)
    ? Math.min(Math.max(requested, 1), 50)
    : 50;
  return context.json({
    status: "ok",
    mode: "operator",
    retention_seconds: config.emailRetentionSeconds,
    messages: await messages.listAll(limit),
  });
});

operatorRoutes.post("/logout", (context) => {
  context.header("Set-Cookie", clearSessionCookie());
  return context.json({ status: "ok" });
});
