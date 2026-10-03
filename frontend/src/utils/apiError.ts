/**
 * Helpers that turn thrown API/stream errors into user-facing messages.
 *
 * `getApiErrorMessage` flattens axios error payloads (FastAPI `detail`, pydantic `msg`/`loc`,
 * generic `message`/`error`/`errors`) and maps 401 and 429 responses to dedicated copy, honouring
 * `Retry-After` when present. `sanitizeAIError` rewrites raw gRPC/AI backend errors, stripping stack
 * traces and internal detail so nothing leaks into the UI.
 *
 * @module utils/apiError
 */

import axios from "axios";

function toFlatMessage(value: unknown): string | null {
  if (typeof value === "string") {
    const trimmed = value.trim();
    return trimmed.length > 0 ? trimmed : null;
  }

  if (Array.isArray(value)) {
    const messages = value
      .map((item) => toFlatMessage(item))
      .filter((item): item is string => Boolean(item));
    return messages.length > 0 ? messages.join("; ") : null;
  }

  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;

    if (typeof record.msg === "string" && record.msg.trim().length > 0) {
      const loc = Array.isArray(record.loc)
        ? record.loc.map((part) => (typeof part === "string" ? part : String(part))).join(".")
        : "";
      return loc ? `${loc}: ${record.msg}` : record.msg;
    }

    const knownKeys = ["detail", "message", "error", "errors"];
    for (const key of knownKeys) {
      const nested = toFlatMessage(record[key]);
      if (nested) {
        return nested;
      }
    }
  }

  return null;
}

export function getApiErrorMessage(
  error: unknown,
  fallback: string = "Something went wrong",
): string {
  if (axios.isAxiosError(error)) {
    if (!error.response) {
      if (error.code === "ERR_NETWORK") {
        return "We are having trouble connecting right now. Check your internet and try again.";
      }
      return error.message || fallback;
    }

    if (error.response.status === 401) {
      return "Your session has expired. Please sign in again.";
    }

    if (error.response.status === 429) {
      const retryAfterRaw = error.response.headers?.["retry-after"];
      const retryAfterValue = Array.isArray(retryAfterRaw) ? retryAfterRaw[0] : retryAfterRaw;
      const retryAfterSeconds = Number(retryAfterValue);

      if (Number.isFinite(retryAfterSeconds) && retryAfterSeconds > 0) {
        return `Too many requests. Please try again in ${Math.ceil(retryAfterSeconds)} seconds.`;
      }
      return "Too many requests right now. Please wait a moment and try again.";
    }

    const payloadMessage = toFlatMessage(error.response.data);
    if (payloadMessage) {
      return payloadMessage;
    }

    return `Request failed (${error.response.status}).`;
  }

  if (error instanceof Error) {
    return error.message || fallback;
  }

  if (typeof error === "string" && error.trim().length > 0) {
    return error;
  }

  return fallback;
}

/**
 * Sanitise raw AI / gRPC error strings into user-friendly messages.
 * Strips stack traces, gRPC status codes, and internal details.
 */
export function sanitizeAIError(raw: string): string {
  if (!raw || typeof raw !== "string")
    return "AI analysis is temporarily unavailable. Please try again.";

  const s = raw.trim();

  // gRPC connectivity / deadline errors
  if (/grpc|UNAVAILABLE|DEADLINE_EXCEEDED|connect\s+ECONNREFUSED/i.test(s)) {
    return "AI analysis service is currently unreachable. The gRPC backend may be restarting — please try again in a moment.";
  }

  // Network / timeout
  if (/timeout|ETIMEDOUT|ERR_NETWORK|fetch failed|network/i.test(s)) {
    return "Request timed out while reaching the AI service. Please try again.";
  }

  // 500 / internal
  if (/internal server error|status\s*:?\s*500/i.test(s)) {
    return "The AI service encountered an internal error. Please try again later.";
  }

  // Stack trace detection — if the string contains file paths or line numbers, strip it
  if (/Traceback|\.py:|\.js:|at\s+\w+\s+\(/i.test(s)) {
    // Try to extract just the last error line
    const lines = s.split("\n").filter((l) => l.trim());
    const lastLine = lines[lines.length - 1]?.trim();
    if (lastLine && !/\.(py|js|ts):/.test(lastLine)) {
      return lastLine;
    }
    return "AI analysis failed due to an internal error. Please try again.";
  }

  // If it's reasonably short and looks human-readable, pass it through
  if (s.length < 200) return s;

  // Truncate long raw errors
  return s.slice(0, 150) + "…";
}
