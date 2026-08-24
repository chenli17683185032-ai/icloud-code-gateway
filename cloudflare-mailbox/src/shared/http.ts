import type { Context } from "hono";
import type { ZodType } from "zod";
import { ValidationError } from "./errors";
import type { WorkerContext } from "./types";

export async function parseJson<T>(
  context: Context<WorkerContext>,
  schema: ZodType<T>,
): Promise<T> {
  const length = Number.parseInt(
    context.req.header("Content-Length") ?? "0",
    10,
  );
  if (Number.isFinite(length) && length > 16 * 1024) {
    throw new ValidationError("请求内容过大。");
  }
  let body: unknown;
  try {
    body = await context.req.json();
  } catch {
    throw new ValidationError("请求必须是 JSON。");
  }
  const result = schema.safeParse(body);
  if (!result.success) throw new ValidationError();
  return result.data;
}

export function requestId(request: Request): string {
  return request.headers.get("cf-ray")?.split("-")[0] || crypto.randomUUID();
}

export function clientIp(request: Request): string {
  return (
    request.headers.get("CF-Connecting-IP") ||
    request.headers.get("X-Real-IP") ||
    "local"
  ).slice(0, 128);
}

export function applySecurityHeaders(response: Response, id: string): Response {
  const headers = new Headers(response.headers);
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("X-Frame-Options", "DENY");
  headers.set("Referrer-Policy", "no-referrer");
  headers.set(
    "Permissions-Policy",
    "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
  );
  headers.set(
    "Content-Security-Policy",
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
      "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
  );
  headers.set("X-Request-ID", id);
  if (response.headers.get("Content-Type")?.includes("text/html")) {
    headers.set("Cache-Control", "no-cache, no-store, must-revalidate");
  }
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

export function noStore(context: Context<WorkerContext>): void {
  context.header("Cache-Control", "no-store");
  context.header("Pragma", "no-cache");
}
