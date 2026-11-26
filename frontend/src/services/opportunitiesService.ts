/**
 * Read-only executable-opportunity ranking (`/api/opportunities`). Nothing here can
 * place, sign or cancel an order, and no edge returned is guaranteed profit.
 *
 * @module services/opportunitiesService
 */

import { apiClient } from "./apiClient";

export type OpportunityStatus = "INDICATIVE" | "VERIFIED" | "STALE" | "INELIGIBLE";

export interface ExecutableOpportunity {
  opportunity_id: string | null;
  market_id: string;
  outcome: "YES" | "NO";
  status: OpportunityStatus;
  reasons: string[];
  forecast_id: string;
  source: string;
  size: string;
  executable_vwap: string | null;
  predicted_probability: string | null;
  fees: string | null;
  slippage: string | null;
  capital_required: string | null;
  net_ev: string | null;
  net_ev_per_dollar: string | null;
  conservative_net_ev: string | null;
  components: Record<string, string | null>;
  book_age_ms: number | null;
  expiry: string | null;
  unmodeled_costs: string[];
  guaranteed_profit: false;
}

export interface OpportunitiesResponse {
  ranked: ExecutableOpportunity[];
  filtered: { reason: string; opportunity: ExecutableOpportunity }[];
  notice: string;
  note?: string;
}

export interface OpportunityFilters {
  strategy: "ai_forecast" | "copy_signal" | "arbitrage";
  shares: string;
  min_edge?: string;
  max_age: number;
  fee_bps?: string;
  fee_version?: string;
}

export const opportunitiesService = {
  async list(filters: OpportunityFilters): Promise<OpportunitiesResponse> {
    return apiClient.get<OpportunitiesResponse>("/api/opportunities", { params: filters });
  },
};
