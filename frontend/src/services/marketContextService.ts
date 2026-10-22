import { apiClient } from "./apiClient";

export interface MarketContext {
  schema_version: "market-context-v1";
  condition_id: string;
  question: string;
  snapshot_id: string;
  observed_at: string;
  source_url: string;
  rules: {
    text: string;
    source_url: string;
    content_hash: string;
    verification: "source_text_only";
    market_end_date: string | null;
    settlement_cutoff: null;
    oracle: null;
  };
  outcomes: { token_id: string; outcome: "YES" | "NO"; indicative_price: number | null }[];
  closed: boolean | null;
  active: boolean | null;
  quote_quality: "indicative" | "unavailable";
  quote_observed_at: null;
  warnings: string[];
}

export const marketContextService = {
  get(conditionId: string): Promise<MarketContext> {
    return apiClient.get(`/api/analysis/markets/${encodeURIComponent(conditionId)}/context`);
  },
};
