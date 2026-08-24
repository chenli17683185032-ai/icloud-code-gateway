import { Hono } from "hono";
import {
  NotFoundError,
  UnauthorizedError,
  ValidationError,
} from "../shared/errors";
import { parseJson } from "../shared/http";
import { safeEqual } from "../shared/security";
import { services } from "../shared/services";
import type { WorkerContext } from "../shared/types";
import {
  controlAliasSchema,
  controlKeySchema,
  controlStateSchema,
} from "../aliases/schema";

function pathEmail(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    throw new ValidationError("邮箱地址不正确。");
  }
}

export const controlRoutes = new Hono<WorkerContext>();

controlRoutes.use("*", async (context, next) => {
  const { config } = services(context.env);
  const authorization = context.req.header("Authorization") ?? "";
  const supplied = authorization.toLowerCase().startsWith("bearer ")
    ? authorization.slice(7).trim()
    : (context.req.header("X-Control-Token") ?? "").trim();
  if (!supplied || !(await safeEqual(config.controlPlaneToken, supplied))) {
    throw new UnauthorizedError("控制面认证失败。");
  }
  await next();
});

controlRoutes.post("/v1/aliases", async (context) => {
  const input = await parseJson(context, controlAliasSchema);
  const result = await services(context.env).aliases.upsert(input);
  return context.json({
    status: "ok",
    id: result.id,
    email: result.email,
    state: result.state,
    has_access_key: result.hasAccessKey,
  });
});

controlRoutes.post("/v1/aliases/by-email/:email/key", async (context) => {
  const input = await parseJson(context, controlKeySchema);
  const result = await services(context.env).aliases.issueKey(
    pathEmail(context.req.param("email")),
    input.access_key,
    input.id,
  );
  return context.json({
    status: "ok",
    alias_id: result.id,
    hint: input.access_key.slice(-6),
  });
});

controlRoutes.delete("/v1/aliases/by-email/:email/key", async (context) => {
  await services(context.env).aliases.revokeKey(
    pathEmail(context.req.param("email")),
  );
  return context.json({ status: "ok" });
});

controlRoutes.post("/v1/aliases/by-email/:email/state", async (context) => {
  const input = await parseJson(context, controlStateSchema);
  const result = await services(context.env).aliases.setState(
    pathEmail(context.req.param("email")),
    input.state,
  );
  return context.json({
    status: "ok",
    email: result.email,
    state: result.state,
  });
});

controlRoutes.delete("/v1/aliases/by-email/:email", async (context) => {
  try {
    await services(context.env).aliases.delete(
      pathEmail(context.req.param("email")),
    );
  } catch (error) {
    if (error instanceof NotFoundError) {
      return context.json({ status: "not_found" }, 404);
    }
    throw error;
  }
  return context.json({ status: "ok" });
});
