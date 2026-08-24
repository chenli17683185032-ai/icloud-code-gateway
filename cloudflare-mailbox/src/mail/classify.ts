export type MessageCategory = "gpt" | "grok" | "other";

const GPT_DOMAINS = ["openai.com", "chatgpt.com", "oaistatic.com"];
const GROK_DOMAINS = ["x.ai", "xai.com", "grok.com"];
const EMAIL_DOMAIN = /@([A-Za-z0-9.-]+[A-Za-z0-9])/g;
const IMPORTANT_MAIL =
  /\b(?:subscription|membership|plan|receipt|invoice|payment|billing|support|case|ticket|appeal|suspend(?:ed|ion)?|disable[ds]?|deactivat(?:ed|ion)|terminat(?:ed|ion)|banned?)\b|开通|订阅|会员|账单|支付|付款|收据|封号|停用|暂停|限制|客服|售后|工单|申诉|支持/iu;

function matchesDomain(value: string, domains: string[]): boolean {
  const normalized = value.toLowerCase();
  for (const match of normalized.matchAll(EMAIL_DOMAIN)) {
    const candidate = match[1];
    if (
      candidate &&
      domains.some(
        (domain) => candidate === domain || candidate.endsWith(`.${domain}`),
      )
    ) {
      return true;
    }
  }
  return false;
}

export function classifyMessage(
  parsedSender: string,
  envelopeFrom: string,
): MessageCategory {
  const sender = `${parsedSender}\n${envelopeFrom}`;
  if (matchesDomain(sender, GPT_DOMAINS)) return "gpt";
  if (matchesDomain(sender, GROK_DOMAINS)) return "grok";
  return "other";
}

export function shouldPreserveMessage(
  category: MessageCategory,
  code: string,
  subject: string,
  body: string,
): boolean {
  if (code) return false;
  if (category === "gpt" || category === "grok") return true;
  return IMPORTANT_MAIL.test(`${subject}\n${body}`);
}
