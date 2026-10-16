/**
 * Cross-venue signal endpoints under `/api/venues`.
 *
 * Reads the cached venue rows the backend refresh loop writes —
 * there is no live venue call anywhere in this service. Every response
 * is signal-only: prices are for comparison, never executable quotes.
 * A mismatch carries both questions verbatim because a wrong match is
 * the main way a fake edge appears, and an adapter that has never
 * fetched shows up as missing data rather than an error.
 *
 * @module services/venueService
 */

import { apiClient } from "./apiClient";

// ── Types ───────────────────────────────────────────────────

export interface VenueMarketRow {
  venue: string;
  market_id: string;
  question: string;
  yes_price: number | null;
  no_price: number | null;
  outcomes: string[];
  volume_24h: number;
  liquidity: number;
  end_date: string | null;
  url: string;
  is_real_money: boolean;
  fetched_at: string | null;
}

export interface VenueMarketsResponse {
  markets: VenueMarketRow[];
  total: number;
}

export interface VenueAdapterHealth {
  name: string;
  enabled: boolean;
  last_fetch_at: string | null;
  last_error: string | null;
  markets: number;
  /**
   * How the venue classifies its own money. Optional: the backend only
   * started sending this alongside the play-money flags, so the frontend
   * keeps the name-based fallback until it does.
   */
  money_class?: "real" | "play";
  /** Convenience mirror of `money_class === "play"`, when sent. */
  is_play_money?: boolean;
}

export interface VenueAdaptersResponse {
  adapters: VenueAdapterHealth[];
  scheduler: Record<string, unknown>;
}

/** One normalized question that more than one venue lists. */
export interface CrossVenueGroup {
  key: string;
  venues: string[];
  questions: Record<string, string>;
  rows: VenueMarketRow[];
  yes_min: number | null;
  yes_max: number | null;
  edge: number | null;
  max_liquidity: number;
}

export interface CrossVenueResponse {
  query: string;
  groups: CrossVenueGroup[];
}

/** How two questions were matched — exact key or fuzzy token overlap. */
export type VenueMatchType = "exact" | "fuzzy";

/** One venue's side of a mismatch, with the question verbatim. */
export interface VenueMismatchLeg {
  venue: string;
  market_id: string;
  question: string;
  yes_price: number | null;
  no_price: number | null;
  liquidity: number;
  url: string;
  is_real_money: boolean;
  fetched_at: string | null;
}

/** The same question priced differently on two venues. */
export interface VenueMismatch {
  venues: string[];
  match_type: VenueMatchType;
  match_score: number;
  /** The cheaper side — the leg a reviewer looks at first. */
  cheap: VenueMismatchLeg;
  /** The richer side. */
  rich: VenueMismatchLeg;
  /** Both questions verbatim, keyed by venue, for human review. */
  questions: Record<string, string>;
  edge: number;
  edge_pct: number;
  low_price: number | null;
  high_price: number | null;
  /** Liquidity of the weaker leg: what actually bounds the trade. */
  min_liquidity: number;
  /** False whenever either leg is play money. */
  executable_signal: boolean;
  /** Days between the two legs' close dates; null when one is unknown. */
  end_date_delta_days: number | null;
  /** True when the horizons differ enough to doubt the match. */
  horizon_mismatch: boolean;
  play_money_venues: string[];
  play_money_only: boolean;
  play_money_reason: string | null;
  newest_fetched_at: string | null;
}

export interface VenueMismatchQuery {
  min_edge?: number;
  min_liquidity?: number;
  include_play_money?: boolean;
  limit?: number;
}

/** One intra-Polymarket complement/spread spot, read from the backend detector. */
export interface IntraPolymarketSignal {
  type: string;
  market_id: string;
  market_title: string;
  prices?: number[];
  total_cost?: number;
  profit_pct?: number;
  spread_pct?: number;
  bid?: number;
  ask?: number;
  detected_at: string;
}

export interface VenueMismatchesResponse {
  mismatches: VenueMismatch[];
  count: number;
  min_edge: number;
  min_liquidity: number;
  include_play_money: boolean;
  /** Intra-Polymarket spots from the existing detector, same read-only surface. */
  intra_polymarket: IntraPolymarketSignal[];
  signal_only: boolean;
  generated_at: string;
}

export interface VenueMarketQuery {
  venue?: string;
  q?: string;
  limit?: number;
}

export interface CrossVenueQuery {
  q: string;
  limit?: number;
}

/**
 * Adapters known to trade play money, used only while the backend omits
 * `money_class` / `is_play_money` from the adapters payload.
 */
const PLAY_MONEY_ADAPTER_NAMES = new Set(["manifold"]);

/** True when the adapter's markets are play money, per the API when it says so. */
export function isPlayMoneyAdapter(adapter: VenueAdapterHealth): boolean {
  if (typeof adapter.is_play_money === "boolean") return adapter.is_play_money;
  if (adapter.money_class) return adapter.money_class === "play";
  return PLAY_MONEY_ADAPTER_NAMES.has(adapter.name);
}

// ── Service ─────────────────────────────────────────────────

export const venueService = {
  async listMarkets(query: VenueMarketQuery = {}): Promise<VenueMarketsResponse> {
    return apiClient.get<VenueMarketsResponse>("/api/venues/markets", { params: query });
  },

  async getAdapters(): Promise<VenueAdaptersResponse> {
    return apiClient.get<VenueAdaptersResponse>("/api/venues/adapters");
  },

  async getCrossVenueMarkets(query: CrossVenueQuery): Promise<CrossVenueResponse> {
    return apiClient.get<CrossVenueResponse>("/api/venues/markets/cross", { params: query });
  },

  /**
   * Cross-venue price gaps. Play-money pairs are excluded unless
   * `include_play_money` is set, and even then they come back with
   * `executable_signal: false` — nothing here is an instruction.
   */
  async getMismatches(query: VenueMismatchQuery = {}): Promise<VenueMismatchesResponse> {
    return apiClient.get<VenueMismatchesResponse>("/api/venues/mismatches", { params: query });
  },
};
