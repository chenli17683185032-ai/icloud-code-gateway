export interface Env {
  DB: D1Database;
  ATTACHMENTS: KVNamespace;
  ASSETS: Fetcher;
  CONTROL_PLANE_TOKEN: string;
  LOOKUP_HMAC_KEY: string;
  DATA_ENCRYPTION_KEY: string;
  SESSION_SIGNING_KEY: string;
  OPERATOR_ACCESS_TOKEN: string;
  EMAIL_RETENTION_SECONDS?: string;
  SESSION_TTL_SECONDS?: string;
  MESSAGE_QUERY_LIMIT?: string;
  MAX_BODY_CHARS?: string;
  MAX_HTML_CHARS?: string;
  MAX_EMAIL_BYTES?: string;
  AUTH_ATTEMPTS_PER_MINUTE?: string;
  INBOX_ADDRESS?: string;
}

export interface RuntimeConfig {
  controlPlaneToken: string;
  lookupHmacKey: string;
  dataEncryptionKey: string;
  sessionSigningKey: string;
  operatorAccessToken: string;
  emailRetentionSeconds: number;
  sessionTtlSeconds: number;
  messageQueryLimit: number;
  maxBodyChars: number;
  maxHtmlChars: number;
  maxEmailBytes: number;
  authAttemptsPerMinute: number;
  inboxAddress: string;
}

export interface AliasSecretPayload {
  email: string;
  label: string;
  note: string;
  senderFilter: string;
}

export interface MessageSecretPayload {
  sender: string;
  subject: string;
  body: string;
  html?: string;
  code: string;
}

export interface AttachmentSecretPayload {
  filename: string;
  mimeType: string;
  disposition: "attachment" | "inline";
  contentId: string;
}

export interface SessionPayload {
  version: 1;
  access: "alias" | "operator";
  aliasDigest: string;
  tokenDigest: string;
  expiresAt: number;
  nonce: string;
}

export type WorkerContext = {
  Bindings: Env;
  Variables: {
    requestId: string;
  };
};
