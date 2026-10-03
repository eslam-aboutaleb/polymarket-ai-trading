/**
 * Market discovery endpoints under `/api/markets` plus per-market trader-positioning analysis.
 *
 * Provides the category list, category browse, and offset-paginated search with optional text query,
 * tag, and sort. `streamTraderAnalysis` consumes an SSE stream whose first event carries aggregate
 * `TraderStats` and whose later events deliver text chunks, returning an `AbortController` for
 * cancellation.
 *
 * @module services/marketsService
 */

import { apiClient } from "./apiClient";
import { streamSSE } from "./sseStream";

// ── Types ──────────────────────────────────────────────────────────

export interface MarketCategory {
  id: string;
  label: string;
  icon: string;
  description: string;
}

export interface BrowseMarket {
  question?: string;
  condition_id?: string;
  market_slug?: string;
  slug?: string;
  outcomePrices?: string | string[];
  bestAsk?: number | string | null;
  bestBid?: number | string | null;
  lastTradePrice?: number | string | null;
  volume24hr?: number;
  volume?: number;
  volumeNum?: number;
  liquidity?: number;
  endDate?: string;
  end_date_iso?: string;
  image?: string;
  _event_title?: string;
  _event_slug?: string;
  _event_image?: string;
  _event_volume?: string;
  _event_liquidity?: string;
  _event_volume_24hr?: number;
  groupItemTitle?: string;
  outcomes?: string[];
  tokens?: { token_id: string; outcome: string }[];
}

export interface SearchMarketsResponse {
  markets: BrowseMarket[];
  total: number;
  offset: number;
  has_more: boolean;
}

export interface TopTrader {
  address: string;
  short_address: string;
  yes_volume: number;
  no_volume: number;
  total_volume: number;
  lean: "YES" | "NO";
}

export interface TraderStats {
  yes_traders: number;
  no_traders: number;
  yes_volume: number;
  no_volume: number;
  total_trades: number;
  side_ratio: { yes: number; no: number };
  top_traders: TopTrader[];
}

export interface TraderAnalysisRequest {
  condition_id: string;
  question: string;
  yes_price: number;
  no_price: number;
  volume_24h: number;
  end_date: string;
}

// ── Service ────────────────────────────────────────────────────────

export const marketsService = {
  /** Get the list of market categories */
  async getCategories(): Promise<{ categories: MarketCategory[] }> {
    return apiClient.get("/api/markets/categories");
  },

  /** Browse markets by category tag */
  async getMarketsByCategory(
    tag: string,
    limit: number = 20,
  ): Promise<{ tag: string; markets: BrowseMarket[]; count: number }> {
    return apiClient.get(`/api/markets/browse?tag=${encodeURIComponent(tag)}&limit=${limit}`);
  },

  /** Search / browse all active markets with optional text query + category filter */
  async searchMarkets(params: {
    q?: string;
    tag?: string;
    limit?: number;
    offset?: number;
    sort?: string;
  }): Promise<SearchMarketsResponse> {
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    if (params.tag) qs.set("tag", params.tag);
    if (params.limit) qs.set("limit", String(params.limit));
    if (params.offset) qs.set("offset", String(params.offset));
    if (params.sort) qs.set("sort", params.sort);
    return apiClient.get(`/api/markets/search?${qs.toString()}`);
  },

  /**
   * Stream AI trader-positioning analysis via SSE.
   *
   * The first SSE event contains `{ trader_stats: TraderStats }`.
   * Subsequent events contain `{ chunk: string }` text fragments.
   * The final event contains `{ done: true }` or `{ error: string }`.
   *
   * Returns an AbortController so the caller can cancel.
   */
  streamTraderAnalysis(
    request: TraderAnalysisRequest,
    callbacks: {
      onStats: (stats: TraderStats) => void;
      onChunk: (text: string) => void;
      onDone: () => void;
      onError: (err: string) => void;
    },
  ): AbortController {
    const controller = new AbortController();

    void streamSSE(
      {
        path: "/api/markets/trader-analysis/stream",
        body: request,
        signal: controller.signal,
      },
      {
        onChunk: callbacks.onChunk,
        onEvent: (payload) => {
          if (payload.trader_stats) {
            callbacks.onStats(payload.trader_stats as TraderStats);
          }
        },
        onDone: () => callbacks.onDone(),
        onError: callbacks.onError,
      },
    ).catch((err: unknown) => {
      callbacks.onError(err instanceof Error ? err.message : "Stream failed");
    });

    return controller;
  },
};
