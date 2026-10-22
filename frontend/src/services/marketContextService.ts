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

export function isMarketContext(value: unknown): value is MarketContext {
  if (!value || typeof value !== "object") return false;
  const context = value as Partial<MarketContext>;
  return (
    context.schema_version === "market-context-v1" &&
    typeof context.condition_id === "string" &&
    /^0x[0-9a-fA-F]{64}$/.test(context.condition_id) &&
    typeof context.question === "string" &&
    typeof context.snapshot_id === "string" &&
    typeof context.source_url === "string" &&
    /^https?:\/\//.test(context.source_url) &&
    typeof context.observed_at === "string" &&
    Number.isFinite(Date.parse(context.observed_at)) &&
    !!context.rules &&
    typeof context.rules.text === "string" &&
    typeof context.rules.content_hash === "string" &&
    Array.isArray(context.warnings) &&
    context.warnings.every((item) => typeof item === "string") &&
    Array.isArray(context.outcomes) &&
    context.outcomes.length === 2 &&
    context.outcomes.every(
      (outcome) =>
        !!outcome &&
        typeof outcome.token_id === "string" &&
        ["YES", "NO"].includes(outcome.outcome) &&
        (outcome.indicative_price === null ||
          (Number.isFinite(outcome.indicative_price) &&
            Number(outcome.indicative_price) >= 0 &&
            Number(outcome.indicative_price) <= 1)),
    )
  );
}

export const marketContextService = {
  async get(conditionId: string): Promise<MarketContext> {
    const context = await apiClient.get<unknown>(
      `/api/analysis/markets/${encodeURIComponent(conditionId)}/context`,
    );
    if (
      !isMarketContext(context) ||
      context.condition_id.toLowerCase() !== conditionId.toLowerCase()
    ) {
      throw new Error("Market source returned an invalid or mismatched context.");
    }
    return context;
  },
};
