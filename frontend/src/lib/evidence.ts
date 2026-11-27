/**
 * Evidence-first status model for research and trade-review screens (#13).
 *
 * Pure functions, no I/O. Every function treats a missing field as "unknown",
 * never as "fine": an opportunity only reaches the `verified` state when every
 * input that the issue lists is present and passing. A positive-edge label is
 * never an earnings claim.
 *
 * @module lib/evidence
 */

export type RuleCertainty = "certain" | "ambiguous" | "unknown";

export interface OpportunityEvidence {
  /** ISO timestamp of the quote the edge was computed from. */
  quoteObservedAt?: string | null;
  /** Oldest acceptable quote age in ms. Defaults to DEFAULT_QUOTE_MAX_AGE_MS. */
  quoteMaxAgeMs?: number;
  /** Hash of the resolved market rules the estimate was made against. */
  ruleHash?: string | null;
  ruleCertainty?: RuleCertainty | null;
  /** False when the quoted token is not the outcome being evaluated. */
  tokenMatchesOutcome?: boolean | null;
  /** Edge after fees and depth-aware slippage, as a fraction (0.03 = 3%). */
  netEdge?: number | null;
  /** Resolved forecasts behind the calibration of this estimate. */
  forecastSampleSize?: number | null;
  minForecastSampleSize?: number;
  resolutionDisputed?: boolean | null;
  /** True when the quote came from a verified exchange source, not an index. */
  sourceVerified?: boolean | null;
}

export const DEFAULT_QUOTE_MAX_AGE_MS = 60_000;
export const DEFAULT_MIN_FORECAST_SAMPLE = 30;

export type BlockerCode =
  | "unknown_rule"
  | "token_mismatch"
  | "stale_quote"
  | "negative_net_edge"
  | "insufficient_forecast_history"
  | "disputed_resolution";

export type MissingCode =
  | "rule_hash"
  | "quote_time"
  | "token_match"
  | "net_edge"
  | "forecast_history"
  | "resolution_status"
  | "source_verification";

export type EvidenceState = "blocked" | "indicative" | "verified";

export interface EvidenceAssessment {
  state: EvidenceState;
  blockers: BlockerCode[];
  missing: MissingCode[];
  /** Only true for `verified` evidence with a strictly positive net edge. */
  positiveNetEdgeAllowed: boolean;
}

export const BLOCKER_TEXT: Record<BlockerCode, string> = {
  unknown_rule: "Market rules are unknown or ambiguous",
  token_mismatch: "Quoted token does not match the evaluated outcome",
  stale_quote: "Quote is stale",
  negative_net_edge: "Net edge after fees is not positive",
  insufficient_forecast_history: "Not enough resolved forecasts to trust calibration",
  disputed_resolution: "Resolution is disputed",
};

export const MISSING_TEXT: Record<MissingCode, string> = {
  rule_hash: "rule hash",
  quote_time: "quote time",
  token_match: "token match",
  net_edge: "net edge",
  forecast_history: "forecast history",
  resolution_status: "resolution status",
  source_verification: "source verification",
};

export function assessEvidence(
  evidence: OpportunityEvidence | null | undefined,
  now: number = Date.now(),
): EvidenceAssessment {
  const e = evidence ?? {};
  const blockers: BlockerCode[] = [];
  const missing: MissingCode[] = [];

  if (e.ruleCertainty === "unknown" || e.ruleCertainty === "ambiguous") {
    blockers.push("unknown_rule");
  } else if (!e.ruleHash || e.ruleCertainty == null) {
    missing.push("rule_hash");
  }

  if (e.tokenMatchesOutcome === false) blockers.push("token_mismatch");
  else if (e.tokenMatchesOutcome == null) missing.push("token_match");

  if (e.quoteObservedAt == null) {
    missing.push("quote_time");
  } else {
    const observed = Date.parse(e.quoteObservedAt);
    const maxAge = e.quoteMaxAgeMs ?? DEFAULT_QUOTE_MAX_AGE_MS;
    // An unparseable time is treated as stale: we cannot prove it is fresh.
    if (Number.isNaN(observed) || now - observed > maxAge) blockers.push("stale_quote");
  }

  if (e.netEdge == null || Number.isNaN(e.netEdge)) missing.push("net_edge");
  else if (e.netEdge <= 0) blockers.push("negative_net_edge");

  if (e.forecastSampleSize == null) {
    missing.push("forecast_history");
  } else if (e.forecastSampleSize < (e.minForecastSampleSize ?? DEFAULT_MIN_FORECAST_SAMPLE)) {
    blockers.push("insufficient_forecast_history");
  }

  if (e.resolutionDisputed === true) blockers.push("disputed_resolution");
  else if (e.resolutionDisputed == null) missing.push("resolution_status");

  if (e.sourceVerified !== true) missing.push("source_verification");

  const state: EvidenceState =
    blockers.length > 0 ? "blocked" : missing.length > 0 ? "indicative" : "verified";
  return {
    state,
    blockers,
    missing,
    positiveNetEdgeAllowed: state === "verified" && (e.netEdge ?? 0) > 0,
  };
}

/** Order lifecycle. Only `filled` may be shown as completed. */
export type FillState =
  "accepted" | "acknowledged" | "partially_filled" | "filled" | "rejected" | "canceled" | "unknown";

export interface FillInfo {
  label: string;
  /** True only when the full size is confirmed filled by the exchange. */
  completed: boolean;
  /** True while the order can still change. */
  pending: boolean;
  description: string;
}

const FILL_INFO: Record<FillState, FillInfo> = {
  accepted: {
    label: "Accepted, not filled",
    completed: false,
    pending: true,
    description: "Our system accepted the order. The exchange has not confirmed it yet.",
  },
  acknowledged: {
    label: "Acknowledged by exchange, not filled",
    completed: false,
    pending: true,
    description: "The exchange has the order. No shares are confirmed filled yet.",
  },
  partially_filled: {
    label: "Partially filled",
    completed: false,
    pending: true,
    description: "Only part of the size is confirmed filled. The rest is still open.",
  },
  filled: {
    label: "Filled (exchange confirmed)",
    completed: true,
    pending: false,
    description: "The exchange confirmed the full size.",
  },
  rejected: {
    label: "Rejected",
    completed: false,
    pending: false,
    description: "The order was rejected. Nothing was filled.",
  },
  canceled: {
    label: "Canceled",
    completed: false,
    pending: false,
    description: "The order was canceled. Only the confirmed filled part, if any, remains.",
  },
  unknown: {
    label: "Status unknown, reconciling",
    completed: false,
    pending: true,
    description: "We cannot confirm this order yet. Do not treat it as filled.",
  },
};

export function normalizeFillState(raw: string | null | undefined): FillState {
  const v = (raw ?? "")
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_");
  switch (v) {
    case "accepted":
    case "submitted":
    case "pending":
      return "accepted";
    case "acknowledged":
    case "live":
    case "open":
    case "matched_pending":
      return "acknowledged";
    case "partially_filled":
    case "partial":
      return "partially_filled";
    case "filled":
    case "confirmed":
      return "filled";
    case "rejected":
    case "failed":
      return "rejected";
    case "canceled":
    case "cancelled":
      return "canceled";
    default:
      return "unknown";
  }
}

export function fillInfo(raw: string | null | undefined): FillInfo & { state: FillState } {
  const state = normalizeFillState(raw);
  return { state, ...FILL_INFO[state] };
}

/** Where a result came from. Color is never the only signal: each has text. */
export type TradingMode = "research" | "shadow" | "paper" | "live_disabled";

export const MODE_INFO: Record<TradingMode, { label: string; description: string; cls: string }> = {
  research: {
    label: "Research only",
    description: "Analysis. No orders are created.",
    cls: "border-sky-500/40 bg-sky-500/10 text-sky-300",
  },
  shadow: {
    label: "Shadow",
    description: "Decisions are recorded against live quotes. No orders are created.",
    cls: "border-violet-500/40 bg-violet-500/10 text-violet-300",
  },
  paper: {
    label: "Paper (notional)",
    description: "Simulated money. Results are not earnings.",
    cls: "border-amber-500/40 bg-amber-500/10 text-amber-300",
  },
  live_disabled: {
    label: "Live disabled",
    description: "Live orders are turned off for this view.",
    cls: "border-zinc-500/40 bg-zinc-500/10 text-zinc-300",
  },
};

export interface CostLine {
  label: string;
  /** USDC; negative reduces the result. */
  amount: number | null;
  /** True when measured by the exchange, false when modeled. */
  measured?: boolean;
}

/** Sums only lines with a known amount, and says whether any were unknown. */
export function sumCostLines(lines: CostLine[]): { total: number; hasUnknown: boolean } {
  let total = 0;
  let hasUnknown = false;
  for (const l of lines) {
    if (l.amount == null || Number.isNaN(l.amount)) hasUnknown = true;
    else total += l.amount;
  }
  return { total, hasUnknown };
}

/** Safe link target for a versioned experiment; null when any part is missing. */
export function experimentHref(
  v: {
    experimentId?: string | null;
    datasetVersion?: string | null;
    feeVersion?: string | null;
  } | null,
): string | null {
  if (!v || !v.experimentId || !v.datasetVersion || !v.feeVersion) return null;
  const q = new URLSearchParams({
    dataset: v.datasetVersion,
    fees: v.feeVersion,
  });
  return `/backtesting?experiment=${encodeURIComponent(v.experimentId)}&${q.toString()}`;
}
