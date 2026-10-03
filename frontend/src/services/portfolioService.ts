/**
 * Wallet portfolio endpoints under `/api/portfolio`.
 *
 * Reports USDC/MATIC balances, open positions, and an aggregate PnL summary, and serves the market
 * lists the UI browses or trades (active, newest, and a combined offset-paginated feed) plus
 * lightweight price-refresh calls for markets and positions. Market types are deliberately loose
 * because fields arrive in both snake_case and camelCase shapes.
 *
 * @module services/portfolioService
 */

import { apiClient } from "./apiClient";

export interface WalletBalance {
  wallet_address: string;
  usdc_balance: number;
  matic_balance: number;
  chain: string;
  error?: string;
}

export interface PortfolioSummary {
  wallet_address: string;
  usdc_balance: number;
  matic_balance: number;
  active_positions: number;
  total_positions: number;
  total_invested: number;
  total_current_value: number;
  total_pnl: number;
  pnl_percentage: number;
  win_rate: number;
  wins_positions_history?: number;
  total_positions_history?: number;
  wins_positions?: number;
  resolved_trades: number;
  positions: PolymarketPosition[];
}

export interface PolymarketPosition {
  market?: string;
  title?: string;
  outcome?: string;
  size: number;
  avgPrice?: number;
  curPrice?: number;
  pnl?: number;
  marketSlug?: string;
  asset?: string;
  asset_id?: string;
  condition_id?: string;
  conditionId?: string;
}

export interface MarketsResponse {
  markets: PolymarketMarket[];
  count: number;
}

export interface CombinedMarketsResponse {
  markets: PolymarketMarket[];
  count: number;
  total: number;
  offset: number;
  has_more: boolean;
}

export interface MarketPriceTick {
  id: string;
  question: string;
  outcomePrices?: string[] | string;
  bestAsk?: number | string | null;
  bestBid?: number | string | null;
  lastTradePrice?: number | string | null;
  volume24hr?: number;
  liquidity?: number;
  slug?: string;
}

export interface PriceRefreshResponse {
  markets: MarketPriceTick[];
  ts: string;
}

export interface PolymarketMarket {
  id?: string;
  question?: string;
  condition_id?: string;
  conditionId?: string;
  clobTokenIds?: string[] | string;
  tokens?: { token_id: string; outcome: string }[];
  volume24hr?: number;
  volumeNum?: number;
  liquidity?: number;
  endDate?: string;
  end_date_iso?: string;
  outcomes?: string[];
  outcomePrices?: string[];
  bestAsk?: number | string | null;
  bestBid?: number | string | null;
  lastTradePrice?: number | string | null;
  slug?: string;
  market_slug?: string;
  image?: string;
  _event_title?: string;
  _event_slug?: string;
  _event_image?: string;
  _event_volume?: string;
  _event_liquidity?: string;
  _event_volume_24hr?: number;
  _event_tags?: Array<{ label?: string; slug?: string; id?: string }>;
  groupItemTitle?: string;
}

export const portfolioService = {
  async getBalance(): Promise<WalletBalance> {
    return apiClient.get<WalletBalance>("/api/portfolio/balance");
  },

  async getPositions(): Promise<{
    wallet_address: string;
    positions: PolymarketPosition[];
    count: number;
  }> {
    return apiClient.get("/api/portfolio/positions");
  },

  async getSummary(): Promise<PortfolioSummary> {
    return apiClient.get<PortfolioSummary>("/api/portfolio/summary");
  },

  async getActiveMarkets(limit: number = 10): Promise<MarketsResponse> {
    return apiClient.get<MarketsResponse>(`/api/portfolio/markets?limit=${limit}`);
  },

  async getNewestMarkets(limit: number = 60): Promise<MarketsResponse> {
    return apiClient.get<MarketsResponse>(`/api/portfolio/markets/newest?limit=${limit}`);
  },

  async getCombinedMarkets(
    limit: number = 60,
    offset: number = 0,
  ): Promise<CombinedMarketsResponse> {
    return apiClient.get<CombinedMarketsResponse>(
      `/api/portfolio/markets/combined?limit=${limit}&offset=${offset}`,
    );
  },

  async refreshPrices(limit: number = 20): Promise<PriceRefreshResponse> {
    return apiClient.get<PriceRefreshResponse>(`/api/portfolio/markets/prices?limit=${limit}`);
  },

  async refreshPositionPrices(): Promise<{
    prices: Record<string, number>;
    ts: string;
  }> {
    return apiClient.get("/api/portfolio/positions/prices");
  },
};
