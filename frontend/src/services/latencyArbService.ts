/**
 * Latency-arbitrage endpoints under `/api/latency-arb`.
 *
 * The live edge board lists BTC/ETH/SOL 5m/15m/1h
 * up/down mispricings (model probability vs market
 * probability) from the last engine cycle, plus the
 * per-user engine configuration and the strategy's
 * trade log.
 *
 * @module services/latencyArbService
 */

import { apiClient } from "./apiClient";

// ── Types ───────────────────────────────────────────────────

export interface LatencyArbConfig {
  enabled: boolean;
  edge_threshold: number;
  max_notional: number;
  symbols: string[];
  windows: number[];
  late_entry: boolean;
  daily_loss_limit: number;
  alert_on_opportunity: boolean;
}

export interface LatencyArbOpportunity {
  symbol: string;
  window_minutes: number;
  window_start_epoch: number;
  window_end_epoch: number;
  side: "up" | "down";
  p_model: number;
  p_market: number;
  edge: number;
  distance: number;
  t_remaining: number;
  sigma: number;
  current_price: number;
  window_open: number;
  condition_id: string;
  question: string;
  token_ids: Record<string, string>;
  prices: Record<string, number>;
  feed_lag_ms: number | null;
  detected_at: string;
  seconds_remaining: number;
}

export interface LatencyArbLatencyStats {
  samples: number;
  feed_lag_p50_ms: number;
  feed_lag_p95_ms: number;
  total_p50_ms: number;
  total_p95_ms: number;
}

export interface LatencyArbEngineStatus {
  running: boolean;
  live_mode: boolean;
  last_cycle_at: string | null;
  cycle_seconds: number;
  symbols: string[];
  windows: number[];
}

export interface LatencyArbOpportunitiesResponse {
  engine: LatencyArbEngineStatus;
  latency: LatencyArbLatencyStats;
  opportunities: LatencyArbOpportunity[];
}

export interface LatencyArbTrade {
  id: number;
  market_id: string;
  token_id: string | null;
  action: string;
  amount: number;
  price: number;
  status: string;
  order_hash: string | null;
  expected_price: number | null;
  expected_size: number | null;
  filled_price: number | null;
  filled_size: number | null;
  fee_paid: number | null;
  slippage_bps: number | null;
  latency_ms: number | null;
  pnl: number | null;
  strategy_source: string | null;
  calculation_details: string | null;
  executed_at: string | null;
  created_at: string | null;
}

export interface LatencyArbTradesResponse {
  trades: LatencyArbTrade[];
  total: number;
  limit: number;
  offset: number;
}

export interface LatencyArbConfigUpdate {
  enabled?: boolean;
  edge_threshold?: number;
  max_notional?: number;
  symbols?: string[];
  windows?: number[];
  late_entry?: boolean;
  daily_loss_limit?: number;
  alert_on_opportunity?: boolean;
}

export interface LatencyArbTradesQuery {
  limit?: number;
  offset?: number;
}

// ── Service ─────────────────────────────────────────────────

export const latencyArbService = {
  async getConfig(): Promise<LatencyArbConfig> {
    return apiClient.get<LatencyArbConfig>("/api/latency-arb/config");
  },

  async updateConfig(config: LatencyArbConfigUpdate): Promise<LatencyArbConfig> {
    return apiClient.post<LatencyArbConfig>("/api/latency-arb/config", config);
  },

  async getOpportunities(): Promise<LatencyArbOpportunitiesResponse> {
    return apiClient.get<LatencyArbOpportunitiesResponse>("/api/latency-arb/opportunities");
  },

  async getTrades(query: LatencyArbTradesQuery = {}): Promise<LatencyArbTradesResponse> {
    return apiClient.get<LatencyArbTradesResponse>("/api/latency-arb/trades", { params: query });
  },
};
