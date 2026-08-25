const CODE_CONTEXT =
  /\b(?:verification|verify|one[- ]time(?:[- ](?:password|code))?|otp|code|passcode|security[- ]code|confirmation(?:[- ]code)?|authentication[- ]code|auth[- ]code|pin|login[- ]code|sign[- ]?in[- ]code|sign[- ]?up[- ]code|access[- ]code|grok|xai|x\.ai)\b|验证码|校验码|动态码|安全(?:码|代码)|认证码|确认码|临时(?:码|代码)|登录(?:码|代码|验证码|校验码)|注册(?:码|代码|验证码|校验码)|邮箱(?:验证码|校验码|认证码|确认码)|一次性(?:密码|代码|验证码)?/giu;
const SIX_DIGIT_CODE = /(?<!\d)(\d{6})(?!\d)/g;
const GROUPED_SIX_DIGIT_CODE = /(?<!\d)(\d{3})[ -](\d{3})(?!\d)/g;
const SPACED_SIX_DIGIT_CODE = /(?<!\d)(\d) (\d) (\d) (\d) (\d) (\d)(?!\d)/g;
const GROK_CODE =
  /(?<![A-Za-z0-9])([A-Za-z0-9]{3})-([A-Za-z0-9]{3})(?![A-Za-z0-9])/gi;
const SPACED_GROK_CODE =
  /(?<![A-Za-z0-9])((?=[A-Za-z0-9]{0,2}\d)[A-Za-z0-9]{3}) ([A-Za-z0-9]{3})(?![A-Za-z0-9])/gi;
const COMPACT_GROK_CODE = /(?<![A-Za-z0-9])([A-Za-z0-9]{6})(?![A-Za-z0-9])/gi;
const GROK_HINT = /(?:^|@|\.)(?:x\.ai|xai\.com|grok\.com|xai)\b|\bgrok\b/i;
const GPT_HINT =
  /(?:^|@|\.)(?:openai\.com|chatgpt\.com|oaistatic\.com)\b|\b(?:openai|chatgpt)\b/i;

interface Candidate {
  distance: number;
  kindRank: number;
  direction: number;
  position: number;
  value: string;
}

interface Span {
  start: number;
  end: number;
}

function spans(pattern: RegExp, value: string): Span[] {
  pattern.lastIndex = 0;
  const result: Span[] = [];
  for (const match of value.matchAll(pattern)) {
    if (match.index === undefined) continue;
    result.push({ start: match.index, end: match.index + match[0].length });
  }
  return result;
}

function compareCandidate(left: Candidate, right: Candidate): number {
  return (
    left.distance - right.distance ||
    left.kindRank - right.kindRank ||
    left.direction - right.direction ||
    left.position - right.position
  );
}

function normalizeSource(value: string): string {
  return value
    .normalize("NFKC")
    .replace(/[\u200b-\u200d\u2060\ufeff]/g, "")
    .replace(/[\u2010-\u2015\u2212]/g, "-");
}

function normalizeGrok(parts: string[]): string {
  const value = parts.join("").toUpperCase();
  if (!/[A-Z]/.test(value) || !/\d/.test(value)) return "";
  return parts.length === 2
    ? `${parts[0]?.toUpperCase()}-${parts[1]?.toUpperCase()}`
    : value;
}

export function extractVerificationCode(
  sender: string,
  subject: string,
  body: string,
): string {
  const original = normalizeSource(`${subject}\n${body}`);
  const text = original.replace(/\s+/g, " ");
  const identity = normalizeSource(`${sender}\n${subject}\n${body}`);
  const grokish = GROK_HINT.test(identity);
  const branded = grokish || GPT_HINT.test(identity);
  const contexts = spans(CODE_CONTEXT, text);
  const candidates: Candidate[] = [];

  const consider = (
    pattern: RegExp,
    kindRank: number,
    window: number,
    normalize: (match: RegExpMatchArray) => string,
    allowWithoutContext = false,
  ): void => {
    pattern.lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
      if (match.index === undefined || !match[1]) continue;
      const value = normalize(match);
      if (!value) continue;
      if (!contexts.length) {
        if (allowWithoutContext) {
          candidates.push({
            distance: window,
            kindRank,
            direction: 0,
            position: match.index,
            value,
          });
        }
        continue;
      }
      const start = match.index;
      const end = start + match[0].length;
      for (const context of contexts) {
        let distance = 0;
        let direction = 0;
        if (context.end <= start) distance = start - context.end;
        else if (end <= context.start) {
          distance = context.start - end;
          direction = 1;
        }
        if (distance <= window) {
          candidates.push({
            distance,
            kindRank,
            direction,
            position: start,
            value,
          });
        }
      }
    }
  };

  consider(
    GROK_CODE,
    grokish ? 0 : 3,
    grokish ? 160 : 80,
    (match) => normalizeGrok([match[1] ?? "", match[2] ?? ""]),
    grokish,
  );
  consider(SPACED_GROK_CODE, grokish ? 1 : 4, grokish ? 160 : 80, (match) =>
    normalizeGrok([match[1] ?? "", match[2] ?? ""]),
  );
  consider(COMPACT_GROK_CODE, grokish ? 2 : 5, grokish ? 120 : 80, (match) =>
    normalizeGrok([match[1] ?? ""]),
  );
  consider(SIX_DIGIT_CODE, grokish ? 3 : 0, 80, (match) => match[1] ?? "");
  consider(GROUPED_SIX_DIGIT_CODE, grokish ? 4 : 1, 80, (match) =>
    [match[1], match[2]].join(""),
  );
  consider(SPACED_SIX_DIGIT_CODE, grokish ? 5 : 2, 80, (match) =>
    match.slice(1, 7).join(""),
  );

  if (!candidates.length && branded) {
    const standaloneDigits = original.match(
      /^\s*(\d{6}|\d{3}[ -]\d{3}|\d(?: \d){5})\s*$/m,
    );
    if (standaloneDigits?.[1]) return standaloneDigits[1].replace(/\D/g, "");
  }
  if (!candidates.length && grokish) {
    const standaloneGrok = original.match(
      /^\s*([A-Za-z0-9]{3})([ -]?)([A-Za-z0-9]{3})\s*$/m,
    );
    if (standaloneGrok?.[1] && standaloneGrok[3]) {
      const parts = standaloneGrok[2]
        ? [standaloneGrok[1], standaloneGrok[3]]
        : [`${standaloneGrok[1]}${standaloneGrok[3]}`];
      return normalizeGrok(parts);
    }
  }
  candidates.sort(compareCandidate);
  return candidates[0]?.value ?? "";
}
