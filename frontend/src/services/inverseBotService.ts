/**
 * Endpoints for the inverse-position bot under `/api/inverse-bot`.
 *
 * Manages the tracked positions the bot auto-reverses: list/upsert configs, disable one, trigger an
 * on-demand evaluation of a single position, and read aggregate metrics. Per-position records carry
 * the last AI signal, confidence, web/X summaries, reversal count for the day, and cooldown state.
 *
 * @module services/inverseBotService
 */

import { apiClient } from "./apiClient";

export type InverseBotSizeOverride = "inherit" | "full_notional" | "fixed_amount";

export type InverseBotStatus = "active" | "cooldown" | "sell_only" | "error";

export interface InverseBotPosition {
  id: number;
  token_id: string;
  condition_id: string;
  market_title: string;
  outcome: string;
  enabled: boolean;
  size_mode_override: InverseBotSizeOverride;
  fixed_amount_override: number | null;
  status: InverseBotStatus;
  last_signal: string | null;
  last_confidence: number | null;
  last_reasoning: string | null;
  last_web_summary: string | null;
  last_x_summary: string | null;
  /**
   * Provenance of the social summary: the pipeline reads a search-engine
   * index ("indexed_web"), never an authenticated X feed. `x_status` is
   * "ok", "no_matches" or "provider_unavailable", and `x_source_label` is the
   * ready-to-render label the API builds from them.
   */
  x_source_type: string | null;
  x_coverage: string | null;
  x_status: string | null;
  x_checked_at: string | null;
  x_source_label: string;
  last_error: string | null;
  last_recommendation: string | null;
  last_alt_outcome: string | null;
  last_alt_token_id: string | null;
  last_evaluated_at: string | null;
  last_reversed_at: string | null;
  reversals_today: number;
  reversals_day: string | null;
  persistence_count: number;
  created_at: string;
  updated_at: string | null;
}

export interface UpsertInverseBotPositionRequest {
  token_id: string;
  condition_id: string;
  market_title: string;
  outcome: string;
  enabled: boolean;
  size_mode_override: InverseBotSizeOverride;
  fixed_amount_override?: number | null;
}

export interface ManualEvaluateResponse {
  success: boolean;
  result?: Record<string, unknown>;
  detail?: string;
}

export interface InverseBotMetrics {
  evaluations_total: number;
  reversals_total: number;
  reversals_failed: number;
  sell_only_total: number;
  mcp_errors_total: number;
  running: boolean;
  last_tick_at: string | null;
  inflight: number;
  interval_seconds: number;
}

export async function listInverseBotPositions(): Promise<InverseBotPosition[]> {
  return apiClient.get<InverseBotPosition[]>("/api/inverse-bot/positions");
}

export async function upsertInverseBotPosition(
  body: UpsertInverseBotPositionRequest,
): Promise<InverseBotPosition> {
  return apiClient.post<InverseBotPosition>("/api/inverse-bot/positions", body);
}

export async function disableInverseBotPosition(positionId: number): Promise<void> {
  await apiClient.delete(`/api/inverse-bot/positions/${positionId}`);
}

export async function evaluateInverseBotPosition(
  positionId: number,
): Promise<ManualEvaluateResponse> {
  return apiClient.post<ManualEvaluateResponse>(
    `/api/inverse-bot/positions/${positionId}/evaluate`,
    {},
  );
}

export async function getInverseBotMetrics(): Promise<InverseBotMetrics> {
  return apiClient.get<InverseBotMetrics>("/api/inverse-bot/metrics");
}

export const inverseBotService = {
  listInverseBotPositions,
  upsertInverseBotPosition,
  disableInverseBotPosition,
  evaluateInverseBotPosition,
  getInverseBotMetrics,
};
