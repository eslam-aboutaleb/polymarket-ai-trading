/**
 * Output hygiene for WebMCP tool results.
 *
 * Market titles, rules text and position labels come from third parties. An
 * agent that reads them must treat them as data, never as instructions. This
 * module strips control and bidi characters, bounds length, and replaces text
 * that looks like an instruction to the agent with an explicit placeholder.
 *
 * @module integrations/webmcp/sanitize
 */

// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g;
const INVISIBLE_CHARS = /[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/g;

const INJECTION_PATTERNS: RegExp[] = [
  /ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)/i,
  /disregard\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)/i,
  /\b(system|developer)\s+(prompt|message|instructions?)\b/i,
  /\byou\s+(are|must|should|will)\s+now\b/i,
  /\b(call|invoke|run|execute|use)\s+(the\s+)?(tool|function)\b/i,
  /\b(place|submit|cancel|post)\s+(an?\s+)?(order|trade|bet)\b/i,
  /\b(private\s+key|seed\s+phrase|mnemonic|api[\s_-]?key|secret)\b/i,
  /<\s*\/?\s*(system|assistant|tool|instructions?)\b/i,
  /\bnew\s+instructions?\b/i,
];

export const REDACTED_TEXT = "[redacted: text resembled an instruction to an AI agent]";

export interface SanitizedText {
  text: string;
  truncated: boolean;
  flagged: boolean;
}

/** True when text reads like an instruction aimed at an agent. */
export function looksLikeInjection(text: string): boolean {
  return INJECTION_PATTERNS.some((pattern) => pattern.test(text));
}

/** Normalise untrusted text: drop control/bidi chars, collapse whitespace, bound length. */
export function sanitizeText(value: unknown, maxLength: number): SanitizedText {
  const raw = typeof value === "string" ? value : value == null ? "" : String(value);
  const cleaned = raw
    .replace(CONTROL_CHARS, "")
    .replace(INVISIBLE_CHARS, "")
    .replace(/\s+/g, " ")
    .trim();
  if (looksLikeInjection(cleaned)) {
    return { text: REDACTED_TEXT, truncated: false, flagged: true };
  }
  if (cleaned.length <= maxLength) return { text: cleaned, truncated: false, flagged: false };
  return { text: `${cleaned.slice(0, maxLength - 1)}…`, truncated: true, flagged: false };
}

/** Only http(s) URLs survive; anything else (javascript:, data:, file:) becomes null. */
export function sanitizeUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    if (url.username || url.password) return null;
    return url.toString().slice(0, 500);
  } catch {
    return null;
  }
}

/** A finite number or null: never NaN/Infinity/strings smuggled through numeric fields. */
export function finiteNumber(value: unknown): number | null {
  const n = typeof value === "string" && value.trim() !== "" ? Number(value) : value;
  return typeof n === "number" && Number.isFinite(n) ? n : null;
}
