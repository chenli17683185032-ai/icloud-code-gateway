import PostalMime, { type Address, type Email } from "postal-mime";

const RECIPIENT_HEADERS = [
  "to",
  "delivered-to",
  "x-original-to",
  "envelope-to",
  "resent-to",
  "x-envelope-to",
  "x-apple-forward-to",
  "x-apple-original-recipient",
] as const;
const EMAIL_TOKEN =
  /[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+[A-Za-z0-9]/g;

function decodeEntity(entity: string): string {
  const named: Record<string, string> = {
    amp: "&",
    apos: "'",
    gt: ">",
    lt: "<",
    nbsp: " ",
    quot: '"',
  };
  if (entity.startsWith("#")) {
    const radix = entity.startsWith("#x") ? 16 : 10;
    const raw = entity.slice(radix === 16 ? 2 : 1);
    const codePoint = Number.parseInt(raw, radix);
    if (
      Number.isInteger(codePoint) &&
      codePoint >= 0 &&
      codePoint <= 0x10ffff
    ) {
      return String.fromCodePoint(codePoint);
    }
  }
  return named[entity.toLowerCase()] ?? `&${entity};`;
}

export function htmlToText(value: string): string {
  return value
    .replace(/<(script|style)[^>]*>[\s\S]*?<\/\1>/gi, " ")
    .replace(/<\s*br\s*\/?>/gi, "\n")
    .replace(/<\/(p|div|li|tr|h[1-6])\s*>/gi, "\n")
    .replace(/<[^>]+>/g, " ")
    .replace(/&([^;]{1,12});/g, (_match, entity: string) =>
      decodeEntity(entity),
    )
    .replace(/\r\n?/g, "\n")
    .replace(/[\t ]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function addresses(value: Address | Address[] | undefined): string[] {
  const values = !value ? [] : Array.isArray(value) ? value : [value];
  const result: string[] = [];
  for (const item of values) {
    if (item.group) result.push(...item.group.map((member) => member.address));
    else result.push(item.address);
  }
  return result;
}

export function formatAddress(value: Address | undefined): string {
  if (!value) return "未知发件人";
  if (value.group) {
    return value.group
      .map((member) => member.name || member.address)
      .join(", ");
  }
  return value.name ? `${value.name} <${value.address}>` : value.address;
}

export function recipientCandidates(email: Email): string[] {
  const result: string[] = [];
  const seen = new Set<string>();
  const push = (candidate: string): void => {
    const normalized = candidate.trim().toLowerCase();
    if (!normalized || seen.has(normalized)) return;
    seen.add(normalized);
    result.push(normalized);
  };

  for (const headerName of RECIPIENT_HEADERS) {
    for (const header of email.headers) {
      if (header.key !== headerName) continue;
      for (const match of header.value.matchAll(EMAIL_TOKEN)) push(match[0]);
    }
  }
  for (const candidate of [
    email.deliveredTo,
    ...addresses(email.to),
    ...addresses(email.cc),
    ...addresses(email.bcc),
  ]) {
    if (candidate) push(candidate);
  }
  return result;
}

export interface ParsedIncomingEmail {
  parsed: Email;
  recipients: string[];
  sender: string;
  subject: string;
  body: string;
}

export async function parseIncomingEmail(
  raw: ReadableStream | ArrayBuffer | Uint8Array | string,
  maxBodyChars: number,
): Promise<ParsedIncomingEmail> {
  const parsed = await PostalMime.parse(raw, {
    attachmentEncoding: "arraybuffer",
    maxHeadersSize: 256 * 1024,
    maxNestingDepth: 40,
    maxRfc822NestingDepth: 2,
  });
  const body = String(
    parsed.text || (parsed.html ? htmlToText(parsed.html) : ""),
  )
    .replace(/\0/g, "")
    .replace(/\r\n?/g, "\n")
    .trim()
    .slice(0, maxBodyChars);
  return {
    parsed,
    recipients: recipientCandidates(parsed),
    sender: formatAddress(parsed.from).slice(0, 500),
    subject: String(parsed.subject || "（无主题）")
      .replace(/[\r\n\0]/g, " ")
      .slice(0, 500),
    body,
  };
}
