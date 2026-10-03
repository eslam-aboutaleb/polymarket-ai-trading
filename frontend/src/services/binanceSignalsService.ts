/**
 * Crypto market-signal and token-ranking endpoints under `/api/binance`.
 *
 * Exposes the aggregated dashboard payload plus individual feeds for smart-money signals, active
 * buys, social hype, trending tokens, smart-money inflow, and the PnL leaderboard, along with token
 * search and detail lookups. Payload types are intentionally permissive because upstream fields vary
 * by provider.
 *
 * @module services/binanceSignalsService
 */

import { apiClient } from "./apiClient";

// ── Types ───────────────────────────────────────────────────────────

export interface SmartMoneySignal {
  tokenAddress?: string;
  tokenSymbol?: string;
  tokenName?: string;
  chain?: string;
  chainId?: string;
  signalType?: string;
  action?: string;
  buyCount?: number;
  sellCount?: number;
  netFlowUsd?: number;
  smartMoneyHolders?: number;
  iconUrl?: string;
  price?: number;
  priceChange24h?: number;
  [key: string]: any;
}

export interface SocialHypeToken {
  tokenSymbol?: string;
  tokenName?: string;
  symbol?: string;
  name?: string;
  rank?: number;
  mentions?: number;
  sentimentScore?: number;
  priceChange24h?: number;
  iconUrl?: string;
  [key: string]: any;
}

export interface TrendingToken {
  tokenSymbol?: string;
  tokenName?: string;
  symbol?: string;
  name?: string;
  rank?: number;
  chain?: string;
  priceUsd?: number;
  price?: number;
  priceChange24h?: number;
  volume24h?: number;
  marketCap?: number;
  iconUrl?: string;
  [key: string]: any;
}

export interface PnlLeader {
  walletAddress?: string;
  address?: string;
  pnl?: number;
  totalPnl?: number;
  winRate?: number;
  tradeCount?: number;
  [key: string]: any;
}

export interface BinanceDashboardData {
  smart_money_signals: SmartMoneySignal[];
  social_hype: SocialHypeToken[];
  trending_tokens: TrendingToken[];
  smart_money_inflow: SmartMoneySignal[];
  pnl_leaderboard: PnlLeader[];
  fetched_at: string;
  enabled: boolean;
}

// ── Service ─────────────────────────────────────────────────────────

class BinanceSignalsService {
  async getDashboard(): Promise<BinanceDashboardData> {
    return apiClient.get<BinanceDashboardData>("/api/binance/dashboard");
  }

  async getSmartMoneySignals(
    chain: string = "ethereum",
    limit: number = 20,
  ): Promise<SmartMoneySignal[]> {
    return apiClient.get<SmartMoneySignal[]>(
      `/api/binance/signals/smart-money?chain=${chain}&limit=${limit}`,
    );
  }

  async getActiveBuySignals(
    chain: string = "ethereum",
    limit: number = 10,
  ): Promise<SmartMoneySignal[]> {
    return apiClient.get<SmartMoneySignal[]>(
      `/api/binance/signals/active-buys?chain=${chain}&limit=${limit}`,
    );
  }

  async getSocialHype(limit: number = 20): Promise<SocialHypeToken[]> {
    return apiClient.get<SocialHypeToken[]>(`/api/binance/rankings/social-hype?limit=${limit}`);
  }

  async getTrendingTokens(limit: number = 20): Promise<TrendingToken[]> {
    return apiClient.get<TrendingToken[]>(`/api/binance/rankings/trending?limit=${limit}`);
  }

  async getSmartMoneyInflow(limit: number = 20): Promise<SmartMoneySignal[]> {
    return apiClient.get<SmartMoneySignal[]>(
      `/api/binance/rankings/smart-money-inflow?limit=${limit}`,
    );
  }

  async getPnlLeaderboard(period: string = "7d", limit: number = 20): Promise<PnlLeader[]> {
    return apiClient.get<PnlLeader[]>(
      `/api/binance/rankings/pnl-leaderboard?period=${period}&limit=${limit}`,
    );
  }

  async searchToken(query: string): Promise<any[]> {
    return apiClient.get<any[]>(`/api/binance/token/search?query=${encodeURIComponent(query)}`);
  }

  async getTokenData(address: string, chain: string = "ethereum"): Promise<any> {
    return apiClient.get<any>(
      `/api/binance/token/data?address=${encodeURIComponent(address)}&chain=${chain}`,
    );
  }
}

export const binanceSignalsService = new BinanceSignalsService();
