import { Hono } from "hono";
import { operatorSessionSchema } from "../aliases/schema";
import {
  NotFoundError,
  RateLimitError,
  UnauthorizedError,
} from "../shared/errors";
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

function pathId(value: string): string {
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(value)) throw new NotFoundError();
  return value;
}

function contentDisposition(filename: string): string {
  const safe = filename
    .replace(/[^\x20-\x7e]/g, "_")
    .replace(/["\\]/g, "_")
    .slice(0, 120);
  const encoded = encodeURIComponent(filename).replace(
    /['()*]/g,
    (character) => `%${character.charCodeAt(0).toString(16).toUpperCase()}`,
  );
  return `attachment; filename="${safe || "attachment"}"; filename*=UTF-8''${encoded}`;
}

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
  const page = await messages.listAll(limit, context.req.query("cursor") ?? "");
  return context.json({
    status: "ok",
    mode: "operator",
    retention_seconds: config.emailRetentionSeconds,
    messages: page.messages,
    next_cursor: page.nextCursor,
    has_more: page.hasMore,
  });
});

operatorRoutes.get("/messages/:messageId/html", async (context) => {
  const { config, messages } = services(context.env);
  await requireOperatorSession(config, context.req.raw);
  const html = await messages.htmlDocument(
    pathId(context.req.param("messageId")),
  );
  return new Response(html, {
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "text/html; charset=utf-8",
      "Content-Security-Policy":
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; " +
        "object-src 'none'; media-src 'none'; connect-src 'none'; frame-src 'none'; " +
        "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox",
      "Referrer-Policy": "no-referrer",
      "X-Frame-Options": "SAMEORIGIN",
    },
  });
});

operatorRoutes.get(
  "/messages/:messageId/attachments/:attachmentId",
  async (context) => {
    const { config, messages } = services(context.env);
    await requireOperatorSession(config, context.req.raw);
    const attachment = await messages.attachment(
      pathId(context.req.param("messageId")),
      pathId(context.req.param("attachmentId")),
    );
    return new Response(attachment.bytes, {
      headers: {
        "Cache-Control": "no-store",
        "Content-Disposition": contentDisposition(attachment.filename),
        "Content-Length": String(attachment.bytes.byteLength),
        "Content-Type": attachment.mimeType,
      },
    });
  },
);

operatorRoutes.post("/logout", (context) => {
  context.header("Set-Cookie", clearSessionCookie());
  return context.json({ status: "ok" });
});
