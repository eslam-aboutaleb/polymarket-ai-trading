/**
 * Client for the multi-step orchestration endpoints under `/api/analysis/orchestrate`.
 *
 * A request fans out into a plan of subtasks (market deep dive, opportunity
 * sweep, trader diligence) that the backend executes step by step and merges
 * into one synthesis. The unary call returns the finished result; the SSE call
 * reports each step as it completes, so the UI can show progress while the
 * plan runs.
 *
 * @module services/orchestrationService
 */

import { apiClient } from "./apiClient";
import { streamSSE } from "./sseStream";
import { getApiErrorMessage } from "../utils/apiError";

// --- Types ---

/** Planner keys the service knows how to decompose into steps. */
export type OrchestrationIntent = "market_deep_dive" | "opportunity_sweep" | "trader_diligence";

export interface OrchestrationStep {
  /** Step name from the planner (e.g. `analyze_market`). */
  step: string;
  ok: boolean;
  /** Backend that executed the step (`llm-chain` or `cli-agent`). */
  backend: string;
  duration_ms: number;
  output?: Record<string, unknown>;
  error?: string;
}

export interface OrchestrateRequest {
  intent: OrchestrationIntent | string;
  payload: Record<string, unknown>;
  /** Optional subset of the planner's steps to run. */
  steps?: string[];
}

export interface OrchestrateResponse {
  success: boolean;
  /** Merged answer: strict JSON when the LLM call succeeded, concatenated
   *  step outputs when it fell back. */
  synthesis: string;
  steps: OrchestrationStep[];
  /** True when any step failed and the answer is degraded. */
  partial: boolean;
  metadata: Record<string, string>;
  backend: string;
}

export interface OrchestrationCallbacks {
  /** Called for each step as it completes. */
  onStep: (step: OrchestrationStep) => void;
  /** Called once with the merged synthesis and whether it is partial. */
  onRecommendation: (synthesis: string, partial: boolean) => void;
  onError: (err: string) => void;
  /** Called once the stream terminated. */
  onDone?: (payload?: Record<string, unknown>) => void;
}

// --- Service ---

export const orchestrationService = {
  /** Run a plan and wait for the full result. */
  async orchestrate(request: OrchestrateRequest): Promise<OrchestrateResponse> {
    return apiClient.post<OrchestrateResponse>("/api/analysis/orchestrate", request);
  },

  /**
   * Stream a plan over SSE: one `step` event per completed subtask, then the
   * synthesis (with its `partial` flag), then the shared `done` event.
   *
   * Returns an AbortController so the caller can cancel.
   */
  streamOrchestrate(
    request: OrchestrateRequest,
    callbacks: OrchestrationCallbacks,
  ): AbortController {
    const controller = new AbortController();

    void streamSSE(
      { path: "/api/analysis/orchestrate/stream", body: request, signal: controller.signal },
      {
        onEvent: (payload) => {
          if (payload.step) {
            callbacks.onStep(payload.step as unknown as OrchestrationStep);
          }
          if (typeof payload.recommendation === "string") {
            callbacks.onRecommendation(payload.recommendation, Boolean(payload.partial));
          }
        },
        onDone: (payload) => callbacks.onDone?.(payload),
        onError: callbacks.onError,
      },
    ).catch((err: unknown) => {
      callbacks.onError(getApiErrorMessage(err, "Stream failed"));
    });

    return controller;
  },
};
