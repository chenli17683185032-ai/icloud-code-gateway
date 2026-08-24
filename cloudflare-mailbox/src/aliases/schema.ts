import { z } from "zod";

const email = z.string().trim().min(3).max(254);
const accessToken = z
  .string()
  .trim()
  .regex(/^icg_[A-Za-z0-9_-]{43}$/);

export const controlAliasSchema = z.object({
  id: z.string().trim().max(64).default(""),
  email,
  label: z.string().trim().max(160).default(""),
  note: z.string().trim().max(500).default(""),
  sender_filter: z.string().trim().max(254).default(""),
  state: z.enum(["active", "inactive"]).default("active"),
  access_key: z.union([accessToken, z.literal("")]).default(""),
});

export const controlKeySchema = z.object({
  access_key: accessToken,
  id: z.string().trim().max(64).default(""),
});

export const controlStateSchema = z.object({
  state: z.enum(["active", "inactive"]),
});

export const mailboxSessionSchema = z.object({
  email: z.union([email, z.literal("")]).default(""),
  token: accessToken,
});

export const operatorSessionSchema = z.object({
  token: accessToken,
});

export type ControlAliasInput = z.infer<typeof controlAliasSchema>;
