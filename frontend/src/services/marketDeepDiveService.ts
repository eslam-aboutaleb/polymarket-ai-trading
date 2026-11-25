import { streamSSE } from "./sseStream";
import { getApiErrorMessage } from "../utils/apiError";

/** Wire types for `market-deep-dive-v2`. Decimals are strings; null always means unknown. */
export type Decision = "investigate" | "no_clear_edge" | "abstain";

export interface DeepDiveEvidence {
  evidence_id: string;
  url: string;
  canonical_url: string;
  publisher: string;
  published_at: string | null;
  retrieved_at: string;
  available_at: string | null;
  source_family: "venue_rules" | "search_excerpt";
  content_hash: string;
  excerpt: string | null;
  verification: "excerpt_matched" | "venue_text" | "unverified";
  limitations: string[];
}

export interface DeepDiveClaim {
  claim_id: string;
  statement: string;
  direction: "supports" | "contradicts" | "context";
  pivotal: boolean;
  cited_evidence_ids: string[];
  independent_source_count: number;
  attribution: "attributed" | "unsupported";
  uncertainty: string | null;
  conflicts: string[];
}

export interface DeepDiveForecast {
  raw_probability: string | null;
  calibrated_probability: string | null;
  interval: [string, string] | null;
  model_id: string | null;
  model_version: string | null;
  forecast_recorded_at: string | null;
  calibration_cohort: string | null;
  resolved_sample_count: number;
  brier: string | null;
  market_baseline_brier: string | null;
  validity_status: "validated" | "unvalidated" | "unavailable";
  validity_reasons: string[];
  label: "validated_model_forecast" | "experimental_unvalidated" | "none";
}

export interface DeepDiveCost {
  token_id: string | null;
  bid: string | null;
  ask: string | null;
  quote_age_ms: number | null;
  requested_size: string | null;
  depth_coverage: string | null;
  vwap: string | null;
  costs: Record<string, string> | null;
  net_ev: string | null;
  net_ev_lower_bound: string | null;
  break_even_probability: string | null;
  validity_status: "executable_modeled" | "indicative" | "stale" | "unavailable";
  validity_reasons: string[];
}

export interface MarketDeepDiveResult {
  schema_version: "market-deep-dive-v2";
  analysis_id: string;
  identity: {
    condition_id: string;
    question: string;
    selected_token_id: string | null;
    selected_outcome: "YES" | "NO" | null;
    token_map: Record<string, string>;
    as_of: string;
    market_status: "active" | "closed" | "inactive" | "unknown";
    source_url: string;
  };
  resolution: {
    raw_rule_text: string;
    oracle: string | null;
    cutoff: string | null;
    comparator: string | null;
    threshold: string | null;
    source_url: string;
    verification: "verified" | "unverified" | "ambiguous";
    warning_codes: string[];
  };
  summary: string;
  key_insights: string[];
  evidence: DeepDiveEvidence[];
  claims: DeepDiveClaim[];
  forecast: DeepDiveForecast | null;
  price_and_cost: DeepDiveCost | null;
  risks: string[];
  invalidation_conditions: string[];
  missing_information: string[];
  decision: Decision;
  reason_codes: string[];
  completeness: {
    sections: Record<string, string>;
    upstream_failures: string[];
    persisted: boolean;
  };
  created_at: string;
  execution_eligible: false;
}

const DECISIONS = ["investigate", "no_clear_edge", "abstain"];

export function isDeepDiveResult(value: unknown): value is MarketDeepDiveResult {
  if (!value || typeof value !== "object") return false;
  const r = value as Partial<MarketDeepDiveResult>;
  return (
    r.schema_version === "market-deep-dive-v2" &&
    typeof r.analysis_id === "string" &&
    typeof r.summary === "string" &&
    typeof r.decision === "string" &&
    DECISIONS.includes(r.decision) &&
    r.execution_eligible === false &&
    !!r.identity &&
    typeof r.identity.condition_id === "string" &&
    !!r.resolution &&
    typeof r.resolution.raw_rule_text === "string" &&
    Array.isArray(r.evidence) &&
    Array.isArray(r.claims) &&
    Array.isArray(r.reason_codes) &&
    Array.isArray(r.missing_information)
  );
}

export interface DeepDiveCallbacks {
  onStage: (stage: string) => void;
  onResult: (result: MarketDeepDiveResult) => void;
  onDone: () => void;
  onError: (message: string, status?: number) => void;
}

const STAGES: Record<string, string> = {
  context_ready: "Market source loaded",
  research_started: "Gathering research…",
  evidence_collected: "Evidence collected",
  claims_checked: "Citations checked",
};

/** Stream a structured deep dive. The server resolves all market data from `conditionId`. */
export function streamMarketDeepDive(
  request: { conditionId: string; selectedTokenId?: string; requestedShares?: string },
  callbacks: DeepDiveCallbacks,
): AbortController {
  const controller = new AbortController();
  let received = false;
  let failed = false;
  const live = () => !failed && !controller.signal.aborted;
  const fail = (message: string) => {
    if (live()) {
      failed = true;
      callbacks.onError(message);
    }
  };
  void streamSSE(
    {
      path: "/api/analysis/market/stream",
      body: {
        output_format: "structured_v2",
        condition_id: request.conditionId,
        selected_token_id: request.selectedTokenId,
        requested_shares: request.requestedShares,
      },
      signal: controller.signal,
    },
    {
      onEvent: (event) => {
        if (!live()) return;
        const type = String(event.type ?? "");
        if (STAGES[type]) callbacks.onStage(STAGES[type]);
        if (type === "analysis_completed") {
          const result = event.result;
          if (
            !isDeepDiveResult(result) ||
            result.identity.condition_id.toLowerCase() !== request.conditionId.toLowerCase()
          ) {
            fail("Deep dive returned an invalid or mismatched result.");
            return;
          }
          received = true;
          callbacks.onResult(result);
        }
      },
      onDone: (event) => {
        if (!live()) return;
        if (!received || !event?.done) {
          fail("Deep dive ended before a complete result arrived.");
          return;
        }
        callbacks.onDone();
      },
      onError: fail,
    },
  ).catch((error: unknown) => {
    if (!controller.signal.aborted) fail(getApiErrorMessage(error, "Deep dive failed"));
  });
  return controller;
}
