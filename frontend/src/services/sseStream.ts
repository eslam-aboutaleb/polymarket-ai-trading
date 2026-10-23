/**
 * Shared Server-Sent Events consumer.
 *
 * Every streaming endpoint on this API (market analysis, opportunity
 * discovery, trader analysis, market streaming, news generation) returns the
 * same envelope:
 *
 *   data: {"chunk": "..."}   incremental text
 *   data: {"error": "..."}   terminal failure
 *   data: {"done": true}     terminal success
 *
 * Each caller previously hand-rolled its own copy of the reader loop, which
 * meant five near-identical 40-line blocks that had already started to drift.
 * `consumeSSEStream` is the single implementation; endpoint-specific payload
 * fields are handled through the optional `onEvent` escape hatch.
 *
 * Streaming is the one place axios cannot help, so `fetchStream` keeps the
 * transport on the same auth footing as `services/apiClient`: cookies, the
 * active-account header, and a single-flight session refresh on 401.
 */

import { API_BASE_URL } from "../config/api";
import { describeApiError, getApiErrorMessage } from "../utils/apiError";
import { getActiveAccountHeader } from "./accountService";
import { refreshSession } from "./apiClient";

/** Callbacks invoked as SSE payloads arrive. */
export interface SSEHandlers {
  /** Invoked for each incremental `chunk` payload. */
  onChunk?: (text: string) => void;
  /** Invoked for every payload that is neither `error` nor `done`. */
  onEvent?: (payload: Record<string, unknown>) => void;
  /** Invoked once on transport failure, HTTP error, or an `error` payload. */
  onError?: (message: string) => void;
  /**
   * Invoked exactly once when the stream terminates, either on a `done` /
   * `all_done` payload (with that payload) or on normal body exhaustion
   * (with `undefined`).
   */
  onDone?: (payload?: Record<string, unknown>) => void;
}

/**
 * Read a response body as a stream of complete lines.
 *
 * This is the low-level primitive shared by both SSE dialects used by this API:
 * the JSON-envelope form consumed by {@link consumeSSEStream}, and the named
 * `event:`/`data:` form used by news generation. Network chunks do not respect
 * line boundaries, so the reader keeps a buffer and only emits lines it knows
 * are complete.
 *
 * @param res - Response whose body should be streamed.
 * @param onLines - Receives each batch of newline-separated complete lines.
 */
export async function streamLines(
  res: Response,
  onLines: (lines: string[]) => boolean | void,
): Promise<void> {
  const reader = res.body?.getReader();
  if (!reader) return;

  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    // The final element is an incomplete line; hold it back until the next
    // chunk completes it.
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    // A `true` return means the consumer wants to stop early.
    if (onLines(lines) === true) return;
  }
}

/**
 * Read an SSE response body and dispatch payloads to the supplied handlers.
 *
 * Terminates on the first `error`, `done` or `all_done` payload, and always
 * invokes `onDone` when the body is exhausted without a terminal payload.
 *
 * @param res - A successful `fetch` response carrying a readable body.
 * @param handlers - Callbacks for chunks, other events, errors and completion.
 */
export async function consumeSSEStream(res: Response, handlers: SSEHandlers): Promise<void> {
  if (!res.ok) {
    const errBody = await res.text().catch(() => "");
    let data: unknown = errBody;
    if (errBody) {
      try {
        data = JSON.parse(errBody);
      } catch {
        // Keep plain-text error responses available to the common formatter.
      }
    }
    const retryAfterHeader = res.headers.get("retry-after");
    const retryAfterValue = Number(retryAfterHeader);
    handlers.onError?.(
      describeApiError(
        {
          status: res.status,
          data,
          retryAfter: Number.isFinite(retryAfterValue) ? retryAfterValue : null,
        },
        `HTTP ${res.status}`,
      ),
    );
    return;
  }

  if (!res.body) {
    handlers.onError?.("No response body");
    return;
  }

  try {
    let terminated = false;
    await streamLines(res, (lines) => {
      terminated = handleSSELines(lines, handlers);
      return terminated;
    });
    if (terminated) return;
  } catch (err) {
    if ((err as Error)?.name === "AbortError") return;
    handlers.onError?.(getApiErrorMessage(err, "Stream failed"));
    return;
  }

  handlers.onDone?.();
}

/**
 * Dispatch a batch of complete SSE lines.
 *
 * @param lines - Complete lines, newline already stripped.
 * @param handlers - Callbacks to dispatch to.
 * @returns `true` when a terminal payload ended the stream.
 */
function handleSSELines(lines: string[], handlers: SSEHandlers): boolean {
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data: ")) continue;

    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(trimmed.slice(6));
    } catch {
      // Malformed payloads are skipped; a partial write is not fatal.
      continue;
    }

    if (parsed.error) {
      handlers.onError?.(String(parsed.error));
      return true;
    }
    if (parsed.done || parsed.all_done) {
      handlers.onDone?.(parsed);
      return true;
    }
    if (typeof parsed.chunk === "string") {
      handlers.onChunk?.(parsed.chunk);
    }
    handlers.onEvent?.(parsed);
  }
  return false;
}

/** Options for the `fetch` calls that open an SSE stream. */
export interface SSEFetchOptions {
  /** Request path, always starting with `/api`. */
  path: string;
  /** JSON request body. */
  body: unknown;
  /** Abort signal from the caller's AbortController. */
  signal: AbortSignal;
}

/** Merge the cookie + active-account credentials onto a stream request. */
function withStreamAuth(init: RequestInit = {}): RequestInit {
  const headers = new Headers(init.headers);
  for (const [name, value] of Object.entries(getActiveAccountHeader())) {
    headers.set(name, value);
  }
  return { ...init, headers, credentials: "include" };
}

/**
 * Open a stream over the authenticated transport.
 *
 * `fetch` is the only way to read a streaming body, so the pieces axios owns
 * are re-applied here: the session cookie, the active-account header, and —
 * on a 401 — the very same single-flight refresh the apiClient response
 * interceptor performs, followed by one replay of the request.
 *
 * A refresh that fails leaves the original 401 response in place: the caller
 * sees the same non-ok response it would have seen without this helper, and
 * the session has already been cleared by `refreshSession`.
 *
 * @param url - Absolute URL to open.
 * @param init - Standard `fetch` init; credentials/headers are merged in.
 */
export async function fetchStream(url: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(url, withStreamAuth(init));
  if (response.status !== 401) return response;

  try {
    await refreshSession();
  } catch {
    return response;
  }
  return fetch(url, withStreamAuth(init));
}

/**
 * POST to an SSE endpoint and stream the response.
 *
 * @param options - Path, body and abort signal for the request.
 * @param handlers - Callbacks for the streamed payloads.
 */
export async function streamSSE(options: SSEFetchOptions, handlers: SSEHandlers): Promise<void> {
  const res = await fetchStream(`${API_BASE_URL}${options.path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(options.body),
    signal: options.signal,
  });
  await consumeSSEStream(res, handlers);
}
