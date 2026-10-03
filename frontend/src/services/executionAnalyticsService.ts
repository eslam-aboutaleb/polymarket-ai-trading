/**
 * Execution analytics endpoints under `/api/trades/analytics`.
 *
 * Surfaces execution quality per strategy over a rolling
 * window: slippage (avg/median), fee drag (Σfees vs gross
 * realized PnL), fill rate, win rate and the Edge Score
 * (0–100, 50 = break-even), plus per-trade expected-vs-actual
 * fill detail.
 *
 * @module services/executionAnalyticsService
 */

import { apiClient } from "./apiClient";

// ── Types ───────────────────────────────────────────────────

export interface ExecutionStrategyStats {
  strategy: string;
  submissions: number;
  filled: number;
  fill_rate: number | null;
  avg_slippage_bps: number | null;
  median_slippage_bps: number | null;
  total_fees: number;
  gross_realized_pnl: number;
  net_realized_pnl: number;
  fee_drag_ratio: number | null;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  avg_fee: number | null;
  avg_risk_per_trade: number | null;
  edge_raw: number | null;
  edge_score: number | null;
  missing_fee: number;
  missing_expected_price: number;
  closed_trades: number;
  wins: number;
  losses: number;
}

export interface ExecutionSummaryResponse {
  user_id: number;
  window_days: number;
  generated_at: string;
  per_strategy: Record<string, ExecutionStrategyStats>;
  totals: ExecutionStrategyStats;
  data_quality: {
    legacy_trades: number;
    missing_expected_price: number;
    missing_fee: number;
  };
}

export interface EdgeScoreRow {
  strategy: string;
  edge_score: number | null;
  edge_raw: number | null;
  win_rate: number | null;
  avg_win: number | null;
  avg_loss: number | null;
  avg_fee: number | null;
  avg_risk_per_trade: number | null;
  filled: number;
  closed_trades: number;
}

export interface EdgeScoreResponse {
  user_id: number;
  window_days: number;
  generated_at: string;
  strategies: EdgeScoreRow[];
}

export interface ExecutionTradeRecord {
  id: number;
  market_id: string;
  token_id: string | null;
  action: string;
  strategy_source: string | null;
  status: string;
  expected_price: number | null;
  expected_size: number | null;
  filled_price: number | null;
  filled_size: number | null;
  fee_paid: number | null;
  slippage_bps: number | null;
  latency_ms: number | null;
  pnl: number | null;
  order_hash: string | null;
  executed_at: string | null;
  created_at: string | null;
}

export interface ExecutionTradesResponse {
  user_id: number;
  strategy: string | null;
  count: number;
  trades: ExecutionTradeRecord[];
}

export interface ExecutionAnalyticsQuery {
  window_days?: number;
}

// ── Service ─────────────────────────────────────────────────

export const executionAnalyticsService = {
  async getSummary(query: ExecutionAnalyticsQuery = {}): Promise<ExecutionSummaryResponse> {
    return apiClient.get<ExecutionSummaryResponse>("/api/trades/analytics/summary", {
      params: query,
    });
  },

  async getEdgeScores(query: ExecutionAnalyticsQuery = {}): Promise<EdgeScoreResponse> {
    return apiClient.get<EdgeScoreResponse>("/api/trades/analytics/edge-score", {
      params: query,
    });
  },

  async getTrades(strategy?: string, limit: number = 50): Promise<ExecutionTradesResponse> {
    const params: Record<string, string | number> = { limit };
    if (strategy) params.strategy = strategy;
    return apiClient.get<ExecutionTradesResponse>("/api/trades/analytics/trades", {
      params,
    });
  },
};
