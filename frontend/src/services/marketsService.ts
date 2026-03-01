import { apiClient } from "./apiClient";
import { API_BASE_URL } from "../config/api";

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
    return apiClient.get(
      `/api/markets/browse?tag=${encodeURIComponent(tag)}&limit=${limit}`,
    );
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

    (async () => {
      try {
        const res = await fetch(
          `${API_BASE_URL}/api/markets/trader-analysis/stream`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            credentials: "include",
            body: JSON.stringify(request),
            signal: controller.signal,
          },
        );

        if (!res.ok) {
          const errBody = await res.text();
          callbacks.onError(errBody || `HTTP ${res.status}`);
          return;
        }

        const reader = res.body?.getReader();
        if (!reader) {
          callbacks.onError("No response body");
          return;
        }

        const decoder = new TextDecoder();
        let buffer = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const trimmed = line.trim();
            if (!trimmed.startsWith("data: ")) continue;
            const jsonStr = trimmed.slice(6);
            try {
              const parsed = JSON.parse(jsonStr);
              if (parsed.error) {
                callbacks.onError(parsed.error);
                return;
              }
              if (parsed.done) {
                callbacks.onDone();
                return;
              }
              if (parsed.trader_stats) {
                callbacks.onStats(parsed.trader_stats);
              }
              if (parsed.chunk) {
                callbacks.onChunk(parsed.chunk);
              }
            } catch {
              // skip malformed JSON
            }
          }
        }
        callbacks.onDone();
      } catch (err: unknown) {
        if ((err as Error).name === "AbortError") return;
        callbacks.onError((err as Error).message || "Stream failed");
      }
    })();

    return controller;
  },
};
