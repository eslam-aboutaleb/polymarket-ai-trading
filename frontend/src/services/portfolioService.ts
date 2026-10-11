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

export interface EquitySnapshotPoint {
  captured_at: string;
  bucket_hour?: string;
  usdc_balance: number;
  unrealized_pnl: number;
  bankroll: number;
  total_invested?: number;
  total_current_value?: number;
  positions_count?: number;
  is_paper?: boolean;
}

export interface EquitySummary {
  period: string;
  start: number;
  end: number;
  delta: number;
  delta_pct: number;
  max_drawdown_pct: number;
  points: number;
}

export interface RiskReportPosition {
  market_title?: string;
  condition_id?: string;
  outcome?: string;
  size: number;
  avg_price?: number;
  current_price?: number;
  value: number;
  weight: number;
  cost_basis: number;
  loss_bound: number;
  concentration_flag: boolean;
}

export interface RiskReport {
  metrics: {
    portfolio_value: number;
    usdc_balance: number;
    unrealized_pnl: number;
    positions_count: number;
    loss_bound: number;
    max_position_weight: number;
    concentration_flags: number;
    clusters: Array<{ label: string; members: string[]; weight: number }>;
    catalysts: Array<{
      market_title: string;
      end_date: string;
      days_to_expiry: number;
    }>;
    risk_limits?: Record<string, number | string | boolean>;
  };
  narrative: {
    overall_risk_rating: string;
    var_estimate: string;
    concentration_flags: string[];
    correlated_clusters: string[];
    tail_scenarios: string[];
    hedges: string[];
  } | null;
  generated_at: string;
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

  async getEquitySnapshots(params?: {
    date?: string;
    from?: string;
    to?: string;
    limit?: number;
    isPaper?: boolean;
  }): Promise<EquitySnapshotPoint[]> {
    const query = new URLSearchParams();
    if (params?.date) query.set("date", params.date);
    if (params?.from) query.set("from", params.from);
    if (params?.to) query.set("to", params.to);
    if (params?.limit) query.set("limit", String(params.limit));
    // Always send the account being charted: an unfiltered payload mixes the
    // paper and the live bankroll on one series.
    if (params?.isPaper !== undefined) query.set("is_paper", String(params.isPaper));
    const suffix = query.toString() ? `?${query.toString()}` : "";
    return apiClient.get<EquitySnapshotPoint[]>(`/api/portfolio/equity/snapshots${suffix}`);
  },

  async getEquitySummary(
    period: "24h" | "7d" | "30d" | "all" = "7d",
    isPaper?: boolean,
  ): Promise<EquitySummary> {
    const query = new URLSearchParams({ period });
    // The summary must describe the same account as the chart beside it.
    if (isPaper !== undefined) query.set("is_paper", String(isPaper));
    return apiClient.get<EquitySummary>(`/api/portfolio/equity/summary?${query.toString()}`);
  },

  async getRiskReport(): Promise<RiskReport> {
    return apiClient.get<RiskReport>("/api/portfolio/risk-report");
  },
};
