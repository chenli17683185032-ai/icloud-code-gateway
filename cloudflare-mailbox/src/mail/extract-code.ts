const CODE_CONTEXT =
  /\b(?:verification|verify|one[- ]time(?:[- ](?:password|code))?|otp|code|passcode|security[- ]code|confirmation(?:[- ]code)?|authentication[- ]code|auth[- ]code|pin|login[- ]code|sign[- ]?in[- ]code|sign[- ]?up[- ]code|access[- ]code|grok|xai|x\.ai)\b|验证码|校验码|动态码|安全(?:码|代码)|认证码|确认码|临时(?:码|代码)|一次性(?:密码|代码|验证码)?/giu;
const SIX_DIGIT_CODE = /(?<!\d)(\d{6})(?!\d)/g;
const GROK_CODE =
  /(?<![A-Za-z0-9])([A-Za-z0-9]{3}-[A-Za-z0-9]{3})(?![A-Za-z0-9])/gi;
const GROK_HINT = /(?:^|@|\.)(?:x\.ai|xai\.com|grok\.com|xai)\b|\bgrok\b/i;

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

export function extractVerificationCode(
  sender: string,
  subject: string,
  body: string,
): string {
  const original = `${subject}\n${body}`;
  const text = original.replace(/\s+/g, " ");
  const grokish = GROK_HINT.test(`${sender}\n${subject}\n${body}`);
  const contexts = spans(CODE_CONTEXT, text);
  const candidates: Candidate[] = [];

  const consider = (
    pattern: RegExp,
    kindRank: number,
    window: number,
    normalize: (value: string) => string,
  ): void => {
    pattern.lastIndex = 0;
    for (const match of text.matchAll(pattern)) {
      if (match.index === undefined || !match[1]) continue;
      const value = normalize(match[1]);
      if (!contexts.length) {
        if (grokish && kindRank === 0) {
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

  consider(GROK_CODE, grokish ? 0 : 1, grokish ? 160 : 80, (value) =>
    value.toUpperCase(),
  );
  consider(SIX_DIGIT_CODE, grokish ? 1 : 0, 80, (value) => value);

  if (!candidates.length && grokish) {
    const standalone = original.match(
      /^\s*([A-Za-z0-9]{3}-[A-Za-z0-9]{3})\s*$/m,
    );
    if (standalone?.[1]) return standalone[1].toUpperCase();
  }
  candidates.sort(compareCandidate);
  return candidates[0]?.value ?? "";
}
