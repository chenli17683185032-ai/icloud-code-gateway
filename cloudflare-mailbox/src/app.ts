import { Hono } from "hono";
import { controlRoutes } from "./control/routes";
import { mailboxRoutes } from "./mailbox/routes";
import { operatorRoutes } from "./operator/routes";
import { loadConfig } from "./shared/config";
import { AppError } from "./shared/errors";
import { applySecurityHeaders, requestId } from "./shared/http";
import { logEvent } from "./shared/logging";
import type { WorkerContext } from "./shared/types";

export const app = new Hono<WorkerContext>();

app.use("*", async (context, next) => {
  const id = requestId(context.req.raw);
  context.set("requestId", id);
  await next();
  context.header("X-Request-ID", id);
});

app.get("/healthz", (context) => context.json({ status: "ok" }));

app.get("/readyz", async (context) => {
  loadConfig(context.env);
  await context.env.DB.prepare("SELECT 1 AS ready").first();
  return context.json({ status: "ok" });
});

app.route("/control", controlRoutes);
app.route("/api/operator", operatorRoutes);
app.route("/api", mailboxRoutes);

app.onError((error, context) => {
  const requestIdValue = context.get("requestId") || crypto.randomUUID();
  const appError =
    error instanceof AppError
      ? error
      : new AppError("internal_error", 500, "服务暂时不可用，请稍后重试。");
  logEvent("http_error", {
    request_id: requestIdValue,
    code: appError.code,
    status: appError.status,
  });
  return context.json(
    {
      status: appError.code,
      message: appError.message,
      request_id: requestIdValue,
    },
    appError.status as 400 | 401 | 404 | 422 | 429 | 500,
    { "Cache-Control": "no-store" },
  );
});

app.notFound(async (context) => {
  if (
    context.req.method === "GET" &&
    ["/admin/mail", "/admin/mail/"].includes(context.req.path)
  ) {
    const assetUrl = new URL(context.req.url);
    assetUrl.pathname = "/admin/index.html";
    return context.env.ASSETS.fetch(
      new Request(assetUrl.toString(), context.req.raw),
    );
  }
  if (
    context.req.path.startsWith("/api/") ||
    context.req.path.startsWith("/control/")
  ) {
    return context.json({ status: "not_found", message: "接口不存在。" }, 404);
  }
  return context.env.ASSETS.fetch(context.req.raw);
});

export async function fetchHandler(
  request: Request,
  env: WorkerContext["Bindings"],
  ctx: ExecutionContext,
) {
  const response = await app.fetch(request, env, ctx);
  return applySecurityHeaders(
    response,
    response.headers.get("X-Request-ID") || requestId(request),
  );
}
