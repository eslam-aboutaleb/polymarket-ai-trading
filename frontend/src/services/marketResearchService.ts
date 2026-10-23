import { isMarketContext, type MarketContext } from "./marketContextService";
import { streamSSE } from "./sseStream";
import { getApiErrorMessage } from "../utils/apiError";

export interface ResearchClaim {
  statement: string;
  polarity: "supporting" | "contradicting";
  source_url: string | null;
  quote: string;
  pivotal: boolean;
  attribution: "venue_excerpt" | "search_excerpt" | "unverified";
}

export interface MarketResearchResult {
  schema_version: "market-research-v1";
  analysis_id: string;
  context: MarketContext;
  status: "investigate" | "insufficient_evidence";
  summary: string;
  claims: ResearchClaim[];
  missing_information: string[];
  reason_codes: string[];
  provider: string | null;
  model: string | null;
  execution_eligible: false;
}

function isResult(value: unknown): value is MarketResearchResult {
  if (!value || typeof value !== "object") return false;
  const result = value as Partial<MarketResearchResult>;
  return (
    result.schema_version === "market-research-v1" &&
    typeof result.analysis_id === "string" &&
    typeof result.summary === "string" &&
    (result.status === "investigate" || result.status === "insufficient_evidence") &&
    result.execution_eligible === false &&
    Array.isArray(result.claims) &&
    result.claims.every(
      (claim) =>
        !!claim &&
        typeof claim.statement === "string" &&
        typeof claim.quote === "string" &&
        ["supporting", "contradicting"].includes(claim.polarity) &&
        ["venue_excerpt", "search_excerpt", "unverified"].includes(claim.attribution),
    ) &&
    Array.isArray(result.missing_information) &&
    result.missing_information.every((v) => typeof v === "string") &&
    Array.isArray(result.reason_codes) &&
    result.reason_codes.every((v) => typeof v === "string") &&
    isMarketContext(result.context)
  );
}

export function streamMarketResearch(
  conditionId: string,
  callbacks: {
    onResult: (result: MarketResearchResult) => void;
    onStatus: (text: string) => void;
    onDone: () => void;
    onError: (message: string) => void;
  },
): AbortController {
  const controller = new AbortController();
  let resultReceived = false;
  let failed = false;
  const fail = (message: string) => {
    if (!failed) {
      failed = true;
      callbacks.onError(message);
    }
  };
  void streamSSE(
    {
      path: "/api/analysis/market/research/stream",
      body: { condition_id: conditionId },
      signal: controller.signal,
    },
    {
      onEvent: (event) => {
        if (failed || controller.signal.aborted) return;
        if (event.type === "context_ready") callbacks.onStatus("Market source loaded");
        if (event.type === "research_started")
          callbacks.onStatus("Gathering research and checking citations…");
        if (event.type === "analysis_completed") {
          if (
            !isResult(event.result) ||
            event.result.context.condition_id.toLowerCase() !== conditionId.toLowerCase()
          ) {
            fail("Research returned an invalid or mismatched result.");
            return;
          }
          resultReceived = true;
          callbacks.onResult(event.result);
        }
      },
      onDone: (event) => {
        if (failed || controller.signal.aborted) return;
        if (!resultReceived || !event?.done) {
          fail("Research ended before a complete result arrived.");
          return;
        }
        callbacks.onDone();
      },
      onError: fail,
    },
  ).catch((error: unknown) => {
    if (!controller.signal.aborted) fail(getApiErrorMessage(error, "Research failed"));
  });
  return controller;
}
