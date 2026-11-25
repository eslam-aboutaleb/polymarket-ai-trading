/**
 * Production WebMCP registry.
 *
 * Registers the read-only tools from `tools.ts` on `navigator.modelContext`
 * (or `document.modelContext`) only when ALL hold: the browser exposes a
 * native WebMCP context (we never ship a polyfill in production), the user has
 * a validated session, and that wallet has opted in. Any change (logout,
 * account switch, opt-out, unmount) aborts every registration and every
 * in-flight call, and results that finish after a change are discarded, so one
 * account's data can never be delivered under another's session.
 *
 * Tool results use one envelope, versioned by `WEBMCP_CONTRACT_VERSION`.
 *
 * @module integrations/webmcp/registry
 */

import { validateInput } from "./schema";
import {
  WEBMCP_CONTRACT_VERSION,
  ToolInputError,
  WEBMCP_TOOLS,
  type ToolResult,
  type WebMcpToolDefinition,
} from "./tools";
import { isApiError } from "../../utils/apiError";

export const TOOL_TIMEOUT_MS = 10_000;
export const MAX_CONCURRENT_CALLS = 2;
export const MAX_CALLS_PER_MINUTE = 30;
export const MAX_RESULT_BYTES = 24_000;

/** The slice of the WebMCP ModelContext API this app uses. */
export interface ModelContextLike {
  registerTool: (
    tool: {
      name: string;
      title?: string;
      description: string;
      inputSchema: unknown;
      annotations?: Record<string, boolean>;
      execute: (input: unknown) => Promise<unknown> | unknown;
    },
    options?: { signal?: AbortSignal },
  ) => unknown;
}

export interface WebMcpSessionKey {
  walletAddress: string;
  accountId: number | null;
}

export interface WebMcpEnvironment {
  getModelContext: () => ModelContextLike | null;
  now: () => number;
  origin: string;
  onCall: (entry: {
    at: string;
    tool: string;
    origin: string;
    inputFields: string[];
    outcome: "ok" | "error";
    errorCode?: string;
    durationMs: number;
  }) => void;
  getSessionKey: () => WebMcpSessionKey | null;
}

export type RegistryDecision =
  | { state: "unsupported" }
  | { state: "signed_out" }
  | { state: "disabled" }
  | { state: "active"; tools: string[] };

export function sessionKeyId(key: WebMcpSessionKey | null): string | null {
  return key ? `${key.walletAddress.toLowerCase()}#${key.accountId ?? "default"}` : null;
}

/** Native-only detection: `navigator.modelContext` first, then `document.modelContext`. */
export function detectModelContext(): ModelContextLike | null {
  const candidates: unknown[] = [];
  try {
    candidates.push((navigator as unknown as { modelContext?: unknown }).modelContext);
  } catch {
    /* ignore */
  }
  try {
    candidates.push((document as unknown as { modelContext?: unknown }).modelContext);
  } catch {
    /* ignore */
  }
  for (const c of candidates) {
    if (c && typeof (c as ModelContextLike).registerTool === "function")
      return c as ModelContextLike;
  }
  return null;
}

function envelope(
  tool: string,
  result: ToolResult | null,
  error: { code: string; message: string } | null,
  now: number,
): string {
  const base = {
    schema_version: WEBMCP_CONTRACT_VERSION,
    tool,
    read_only: true,
    generated_at: new Date(now).toISOString(),
  };
  if (error || !result) {
    return JSON.stringify({ ...base, ok: false, error: error ?? { code: "unknown", message: "" } });
  }
  const body = JSON.stringify({
    ...base,
    ok: true,
    mode: result.mode,
    provenance: {
      source: result.source,
      source_timestamp: result.sourceTimestamp,
      data_quality: result.dataQuality,
    },
    truncated: result.truncated,
    redacted_fields: result.flaggedFields,
    data: result.data,
  });
  if (body.length > MAX_RESULT_BYTES) {
    return JSON.stringify({
      ...base,
      ok: false,
      error: {
        code: "result_too_large",
        message: "Result exceeded the size limit; request fewer items.",
      },
    });
  }
  return body;
}

function errorCodeOf(err: unknown): { code: string; message: string } {
  if (err instanceof ToolInputError) return { code: err.code, message: err.message };
  if (isApiError(err)) {
    const status = (err as { status?: number }).status;
    if (status === 401)
      return { code: "unauthorized", message: "Session expired or not signed in." };
    if (status === 403) return { code: "forbidden", message: "Not permitted for this account." };
    if (status === 404) return { code: "not_found", message: "Source not found." };
    if (status === 429) return { code: "rate_limited", message: "Source rate limit reached." };
    return { code: "source_error", message: "The data source returned an error." };
  }
  return { code: "source_error", message: "The data source failed." };
}

export class WebMcpRegistry {
  private controller: AbortController | null = null;
  private activeKey: string | null = null;
  private generation = 0;
  private inflight = 0;
  private callTimes: number[] = [];
  private registeredContext: ModelContextLike | null = null;

  constructor(
    private readonly env: WebMcpEnvironment,
    private readonly tools: readonly WebMcpToolDefinition[] = WEBMCP_TOOLS,
  ) {}

  /** Re-evaluate policy. Idempotent; registers, re-registers or tears down as needed. */
  sync(optedIn: boolean): RegistryDecision {
    const context = this.env.getModelContext();
    if (!context) {
      this.teardown();
      return { state: "unsupported" };
    }
    const key = this.env.getSessionKey();
    const keyId = sessionKeyId(key);
    if (!keyId) {
      this.teardown();
      return { state: "signed_out" };
    }
    if (!optedIn) {
      this.teardown();
      return { state: "disabled" };
    }
    if (this.controller && this.activeKey === keyId && this.registeredContext === context) {
      return { state: "active", tools: this.tools.map((t) => t.name) };
    }
    this.teardown();
    this.register(context, keyId);
    return { state: "active", tools: this.tools.map((t) => t.name) };
  }

  /** Abort all registrations and in-flight calls. Safe to call repeatedly. */
  teardown(): void {
    if (this.controller) {
      this.controller.abort();
      this.controller = null;
    }
    this.activeKey = null;
    this.registeredContext = null;
    this.generation += 1;
  }

  get isActive(): boolean {
    return this.controller !== null;
  }

  private register(context: ModelContextLike, keyId: string): void {
    const controller = new AbortController();
    this.controller = controller;
    this.activeKey = keyId;
    this.registeredContext = context;
    const generation = this.generation;
    for (const tool of this.tools) {
      try {
        const pending = context.registerTool(
          {
            name: tool.name,
            title: tool.title,
            description: `${tool.description} [${WEBMCP_CONTRACT_VERSION}]`,
            inputSchema: tool.inputSchema,
            annotations: { readOnlyHint: true },
            execute: (input) => this.execute(tool, input, generation, keyId, controller.signal),
          },
          { signal: controller.signal },
        );
        if (pending && typeof (pending as Promise<unknown>).catch === "function") {
          (pending as Promise<unknown>).catch(() => undefined);
        }
      } catch {
        // A failed registration must never break the app; the tool is simply absent.
      }
    }
  }

  private allowCall(): { ok: true } | { ok: false; code: string; message: string } {
    const now = this.env.now();
    this.callTimes = this.callTimes.filter((t) => now - t < 60_000);
    if (this.inflight >= MAX_CONCURRENT_CALLS) {
      return { ok: false, code: "busy", message: "Too many concurrent tool calls." };
    }
    if (this.callTimes.length >= MAX_CALLS_PER_MINUTE) {
      return { ok: false, code: "rate_limited", message: "Tool call rate limit reached." };
    }
    this.callTimes.push(now);
    return { ok: true };
  }

  private async execute(
    tool: WebMcpToolDefinition,
    input: unknown,
    generation: number,
    keyId: string,
    registrationSignal: AbortSignal,
  ): Promise<string> {
    const started = this.env.now();
    const inputFields =
      input && typeof input === "object" && !Array.isArray(input)
        ? Object.keys(input as object)
            .slice(0, 10)
            .map((k) => k.slice(0, 40))
        : [];
    const finish = (
      result: ToolResult | null,
      error: { code: string; message: string } | null,
    ): string => {
      const end = this.env.now();
      this.env.onCall({
        at: new Date(end).toISOString(),
        tool: tool.name,
        origin: this.env.origin,
        inputFields,
        outcome: error ? "error" : "ok",
        errorCode: error?.code,
        durationMs: Math.max(0, end - started),
      });
      return envelope(tool.name, result, error, end);
    };

    // Session must still be the one that registered the tools.
    if (
      this.generation !== generation ||
      registrationSignal.aborted ||
      sessionKeyId(this.env.getSessionKey()) !== keyId
    ) {
      return finish(null, {
        code: "session_changed",
        message: "Tools are not active for this session.",
      });
    }
    const validation = validateInput(tool.inputSchema, input);
    if (!validation.ok) return finish(null, { code: "invalid_input", message: validation.message });
    const gate = this.allowCall();
    if (!gate.ok) return finish(null, { code: gate.code, message: gate.message });

    const callController = new AbortController();
    const abortCall = () => callController.abort();
    registrationSignal.addEventListener("abort", abortCall, { once: true });
    const timer = setTimeout(abortCall, TOOL_TIMEOUT_MS);
    this.inflight += 1;
    try {
      const result = await Promise.race([
        tool.run(validation.value, { signal: callController.signal, now: this.env.now }),
        new Promise<never>((_, reject) => {
          callController.signal.addEventListener("abort", () =>
            reject(new ToolInputError("cancelled_or_timeout", "Call cancelled or timed out.")),
          );
        }),
      ]);
      // Account boundary: drop data that finished after logout / switch.
      if (
        this.generation !== generation ||
        registrationSignal.aborted ||
        sessionKeyId(this.env.getSessionKey()) !== keyId
      ) {
        return finish(null, {
          code: "session_changed",
          message: "Session changed; result discarded.",
        });
      }
      return finish(result, null);
    } catch (err) {
      if (
        this.generation !== generation ||
        registrationSignal.aborted ||
        sessionKeyId(this.env.getSessionKey()) !== keyId
      ) {
        return finish(null, {
          code: "session_changed",
          message: "Session changed; call cancelled.",
        });
      }
      return finish(null, errorCodeOf(err));
    } finally {
      clearTimeout(timer);
      registrationSignal.removeEventListener("abort", abortCall);
      this.inflight -= 1;
    }
  }
}
