import { fetchHandler } from "./app";
import { logEvent } from "./shared/logging";
import { services } from "./shared/services";
import type { Env } from "./shared/types";

const worker: ExportedHandler<Env> = {
  fetch: fetchHandler,

  async email(message, env): Promise<void> {
    const result = await services(env).messages.ingest({
      raw: message.raw,
      rawSize: message.rawSize,
      envelopeFrom: message.from,
      envelopeTo: message.to,
    });
    logEvent("email_ingested", {
      stored: result.stored,
      matched: result.matched,
      duplicate: result.duplicate,
    });
  },

  async scheduled(_event, env): Promise<void> {
    const now = Math.floor(Date.now() / 1000);
    const { messages, rateLimits } = services(env);
    const removed = await messages.cleanup(now);
    await rateLimits.cleanup(now - 2 * 60 * 60);
    logEvent("scheduled_cleanup", { removed });
  },
};

export default worker;
