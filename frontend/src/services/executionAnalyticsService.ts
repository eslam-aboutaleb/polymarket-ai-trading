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
  total_fees: number | null;
  known_fees?: number;
  gross_realized_pnl: number;
  net_realized_pnl: number | null;
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

export interface StrategyBreakdownRow {
  strategy: string;
  submissions: number;
  filled: number;
  closed_trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  gross_realized_pnl: number;
  net_realized_pnl: number | null;
  total_fees: number | null;
  known_fees?: number;
  avg_slippage_bps: number | null;
  edge_score: number | null;
}

export interface StrategyBreakdownResponse {
  user_id: number;
  window_days: number;
  generated_at: string;
  strategies: StrategyBreakdownRow[];
  data_quality: {
    unattributed_trades: number;
    strategies: number;
  };
}

export interface ExecutionAnalyticsQuery {
  window_days?: number;
}

export interface LiveBalanceReference {
  id: string;
  observed_at: string;
}
export interface LiveReconciliationReport {
  schema_version: "live-attribution-v2";
  status: "reconciled" | "abstain";
  account_id: number;
  input_hash: string;
  report_id?: string;
  reasons: string[];
  source_ids: number[];
  net_realized_pnl: string | null;
  total_fees_usdc: string | null;
  confirmed_rebates_usdc: string | null;
  estimated_rebates_usdc: string;
  cash_residual_usdc: string | null;
  full_wallet_reconciled: boolean;
  gross_realized_pnl: string | null;
  verified_net_realized_pnl: string | null;
  unrealized_pnl: string | null;
  mark_contribution: string | null;
  benchmark_version: string;
  acceptance_complete: boolean;
  acceptance: { criterion: string; status: "pass" | "incomplete" | "fail"; reason: string }[];
  position_residuals: Record<string, string>;
  history_source_ids: number[];
  benchmarks: {
    benchmark_id: string;
    source_id: number;
    status: "observed_comparison" | "abstain";
    capture_id: string | null;
    implementation_shortfall: string | null;
    spread_cost: string | null;
    execution_difference: string | null;
    reason: string | null;
  }[];
  external_flows: {
    quality: string;
    funding_cashflow: string | null;
    unattributed_rebates: string | null;
    other_income: string | null;
    reasons: string[];
  };
  confidence: {
    release_decision: string;
    comparable_sample_count: number;
    forward_event_count: number;
  };
}

// ── Service ─────────────────────────────────────────────────

export const executionAnalyticsService = {
  async freezeLiveReconciliation(
    opening_id: string,
    closing_id: string,
  ): Promise<LiveReconciliationReport> {
    return apiClient.post<LiveReconciliationReport>(
      "/api/trades/analytics/reconciliation-reports",
      { opening_id, closing_id },
    );
  },
  async getBalanceSnapshots(): Promise<LiveBalanceReference[]> {
    return apiClient.get<LiveBalanceReference[]>("/api/trades/analytics/balance-snapshots");
  },
  async captureBalanceSnapshot(): Promise<LiveBalanceReference> {
    return apiClient.post<LiveBalanceReference>("/api/trades/analytics/balance-snapshots");
  },
  async getLiveReconciliation(
    opening_id: string,
    closing_id: string,
  ): Promise<LiveReconciliationReport> {
    return apiClient.get<LiveReconciliationReport>("/api/trades/analytics/reconciliation", {
      params: { opening_id, closing_id },
    });
  },
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

  async getStrategyBreakdown(
    query: ExecutionAnalyticsQuery = {},
  ): Promise<StrategyBreakdownResponse> {
    return apiClient.get<StrategyBreakdownResponse>("/api/trades/analytics/by-strategy", {
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
