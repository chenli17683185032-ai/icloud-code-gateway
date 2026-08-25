import { AliasRepository } from "../aliases/repository";
import { AliasService } from "../aliases/service";
import { MessageRepository } from "../messages/repository";
import { MessageService } from "../messages/service";
import { RateLimitRepository } from "../rate-limit/repository";
import { loadConfig } from "./config";
import type { Env } from "./types";

export function services(env: Env) {
  const config = loadConfig(env);
  const aliases = new AliasService(new AliasRepository(env.DB), config);
  const messages = new MessageService(
    new MessageRepository(env.DB),
    aliases,
    env.ATTACHMENTS,
    config,
  );
  const rateLimits = new RateLimitRepository(env.DB);
  return { config, aliases, messages, rateLimits };
}
