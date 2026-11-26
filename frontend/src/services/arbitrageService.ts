/**
 * Read-only arbitrage candidates and paper-only feasibility under `/api/v1/arbitrage`.
 *
 * Candidates from the scanner are INDICATIVE. Feasibility reads frozen native book receipts at an
 * explicit `as_of`; nothing here places, signs or cancels an order.
 *
 * @module services/arbitrageService
 */

import { apiClient } from "./apiClient";

export type ArbCandidateState =
  "INDICATIVE" | "DEPTH_VERIFIED" | "EXECUTION_READY" | "STALE" | "INVALID" | "PARTIAL";

export interface ArbLegQuote {
  label: string;
  venue: string;
  token_id: string;
  outcome: string;
  vwap: string | null;
  matched_shares: string;
  net_cash: string | null;
  book_hash: string | null;
  book_as_of: string;
}

export interface ArbNetBound {
  size: string;
  worst_case_payoff: string;
  quote_gross_gap: string;
  fee_inclusive_bound: string;
  net_bound: string;
  unknown_costs: string[];
  capital_required: string;
}

export interface ArbOneLegCase {
  filled_leg: string;
  failed_leg: string;
  fill_fraction: string;
  cash_spent: string;
  bound: string;
  unwind_status: "modeled" | "unavailable";
  modeled_unwind_loss: string | null;
}

export interface ArbCandidate {
  candidate_id: string;
  kind: "paired_outcome" | "cross_venue";
  state: ArbCandidateState;
  market_title?: string;
  label?: string;
  gross_price_gap_pct_points?: number;
  condition_ids: string[];
  as_of: string;
  stale_at: string | null;
  requested_size: string;
  size: string;
  max_executable_size: string;
  max_profitable_size: string | null;
  leg_quotes: ArbLegQuote[];
  net_bound: ArbNetBound | null;
  executable_profit: string | null;
  cost_waterfall: Array<{ item: string; amount: string }>;
  equivalence: { status: string; reasons: string[] } | null;
  settlement_risk: string[];
  partial_fill_risk: {
    max_loss_bound: string;
    non_atomic: boolean;
    cases: ArbOneLegCase[];
  } | null;
  badges: string[];
  reasons: string[];
  notices: string[];
  input_digest: string;
  paper_only: boolean;
  live_execution_enabled: boolean;
  profit_assured: boolean;
}

export interface ArbCandidatesResponse {
  schema_version: string;
  engine_version: string;
  count: number;
  candidates: ArbCandidate[];
  paper_only: boolean;
  live_execution_enabled: boolean;
}

export interface FeasibilityInput {
  size: string;
  fee_bps?: string;
  fee_version?: string;
  costs: { gas_usdc?: string; settlement_usdc?: string };
  as_of?: string;
}

export interface FeasibilityResponse {
  candidate: ArbCandidate;
  lineage?: {
    as_of: string;
    capture_ids: string[];
    source: string;
    credentials_included: boolean;
  };
}

export const arbitrageService = {
  async listCandidates(): Promise<ArbCandidatesResponse> {
    return apiClient.get<ArbCandidatesResponse>("/api/v1/arbitrage/candidates");
  },

  async feasibility(candidateId: string, body: FeasibilityInput): Promise<FeasibilityResponse> {
    return apiClient.post<FeasibilityResponse>(
      `/api/v1/arbitrage/${encodeURIComponent(candidateId)}/feasibility`,
      body,
    );
  },
};
