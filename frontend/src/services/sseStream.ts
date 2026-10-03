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
 */

import { API_BASE_URL } from "../config/api";

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
    handlers.onError?.(errBody || `HTTP ${res.status}`);
    return;
  }

  if (!res.body) {
    handlers.onError?.("No response body");
    return;
  }

  try {
    await streamLines(res, (lines) => handleSSELines(lines, handlers));
  } catch (err) {
    if ((err as Error)?.name === "AbortError") return;
    handlers.onError?.(err instanceof Error ? err.message : String(err));
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

/**
 * POST to an SSE endpoint and stream the response.
 *
 * @param options - Path, body and abort signal for the request.
 * @param handlers - Callbacks for the streamed payloads.
 */
export async function streamSSE(options: SSEFetchOptions, handlers: SSEHandlers): Promise<void> {
  const res = await fetch(`${API_BASE_URL}${options.path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(options.body),
    signal: options.signal,
  });
  await consumeSSEStream(res, handlers);
}
