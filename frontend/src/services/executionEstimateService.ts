/**
 * Read-only execution cost estimate and shortfall quality (`/api/execution`).
 *
 * Nothing here can place, sign or cancel an order. Estimates are counterfactual
 * walks of displayed book depth and expire with the captured quote.
 *
 * @module services/executionEstimateService
 */

import { apiClient } from "./apiClient";

export type EstimateMode = "MARKETABLE_LIMIT" | "FOK" | "MAKER_REST";

export interface EstimateRequestBody {
  order_intent_id: string;
  mode: EstimateMode;
  side: "BUY" | "SELL";
  shares: string;
  limit_price?: string | null;
  max_slippage_bps?: string | null;
  fee_bps?: string | null;
  fee_version?: string | null;
  maker_fee_bps?: string | null;
}

export interface ExecutionEstimate {
  estimate_id: string | null;
  status: "modeled" | "rejected" | "abstain";
  reason: string | null;
  mode: EstimateMode;
  side: "BUY" | "SELL";
  quantity: string;
  fillable_quantity: string;
  residual_quantity: string | null;
  fill_status: "FULL" | "PARTIAL" | "NONE" | "UNMODELED";
  best_bid: string | null;
  best_ask: string | null;
  arrival_mid: string | null;
  executable_vwap: string | null;
  gross_notional: string | null;
  fees: string | null;
  net_cash: string | null;
  depth_coverage: string;
  expected_price_impact: string | null;
  price_impact_basis: string;
  expected_cost_vs_mid_bps: string | null;
  quote_expiry: string | null;
  max_acceptable_price: string | null;
  book_age_ms: number | null;
  confidence: string;
  live_order_action: false;
}

export interface Revalidation {
  valid: boolean;
  reasons: string[];
}

export interface EstimateResponse {
  status: "modeled" | "rejected" | "abstain";
  reason: string | null;
  record_id?: string;
  estimate: ExecutionEstimate | null;
  prior: Revalidation | null;
}

export interface QualityRow {
  estimate_record_id: string;
  trade_id: number;
  order_hash: string | null;
  benchmark_at: string;
  arrival_mid: string;
  estimated_vwap: string;
  actual_vwap: string | null;
  fill_rate: string;
  gap: {
    status: "compared" | "abstain";
    reason: string | null;
    error_bps: string | null;
    estimated_price_shortfall_bps: string | null;
    actual_price_shortfall_bps: string | null;
  };
  waterfall: {
    benchmark_notional: string;
    estimated_price_cost: string | null;
    actual_price_cost: string;
    fee: string | null;
    missed_trade_cost: string | null;
    components_missing: string[];
  };
}

export interface QualityResponse {
  window_days: number;
  summary: {
    estimates_modeled: number;
    matched_settled: number;
    unmatched_estimates: number;
    ambiguous_estimates: number;
    matched_but_not_settled: number;
    mean_fill_rate: string | null;
    mean_error_bps: string | null;
    p50_error_bps: string | null;
    p95_error_bps: string | null;
    percentile_min_sample: number;
  };
  rows: QualityRow[];
  matching_rule: string;
  fees_note: string;
  profitability_claim: false;
}

export const executionEstimateService = {
  async estimate(body: {
    condition_id: string;
    outcome: "YES" | "NO";
    request: EstimateRequestBody;
    prior_estimate_record_id?: string;
  }): Promise<EstimateResponse> {
    return apiClient.post<EstimateResponse>("/api/execution/estimate", body);
  },
  async quality(windowDays = 30): Promise<QualityResponse> {
    return apiClient.get<QualityResponse>("/api/execution/quality", {
      params: { window_days: windowDays },
    });
  },
};
