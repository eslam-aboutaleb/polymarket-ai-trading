import { apiClient } from "./apiClient";

// --- Trade History Types ---

export interface TradeRecord {
  id?: string;
  market?: string;
  market_slug?: string;
  condition_id?: string;
  outcome?: string;
  side?: string;
  size: number;
  price: number;
  type?: string;
  status?: string;
  timestamp?: string;
  fee: number;
  fee_rate_bps?: number;
  pnl?: number | null;
  transaction_hash?: string;
  maker_address?: string;
  trader_side?: string;
}

export interface TradeHistoryResponse {
  wallet_address: string;
  trades: TradeRecord[];
  count: number;
  has_more: boolean;
}

// --- Leaderboard Types ---

export interface LeaderboardEntry {
  rank: number;
  address: string;
  display_name?: string;
  profit_loss: number;
  volume: number;
  markets_traded: number;
  win_rate?: number | null;
  positions_value: number;
  pnl_24h: number;
  pnl_7d: number;
  pnl_30d: number;
  volume_24h: number;
  profile_image?: string | null;
  is_followed: boolean;
  is_notification_followed: boolean;
}

export interface LeaderboardResponse {
  entries: LeaderboardEntry[];
  period: string;
  updated_at: string;
  total: number;
}

// --- Trader Profile ---

export interface TraderProfile {
  wallet_address: string;
  display_name?: string;
  profit_loss: number;
  volume: number;
  markets_traded: number;
  win_rate?: number | null;
  positions: Record<string, unknown>[];
  recent_trades: Record<string, unknown>[];
  profile_image?: string | null;
  trade_stats?: {
    total_trades: number;
    winning_trades: number;
    losing_trades: number;
    win_rate: number;
    total_volume: number;
    avg_trade_size: number;
    largest_trade: number;
    unique_markets: number;
    active_days: number;
    trade_frequency: string;
    market_categories: Record<string, number>;
    first_trade_date?: string;
    last_trade_date?: string;
  } | null;
}

// --- Followed Trader ---

export interface FollowedTrader {
  id: number;
  trader_wallet: string;
  trader_alias?: string | null;
  display_name?: string;
  is_active: boolean;
  max_position_size?: number | null;
  sizing_mode: "inherit_global" | "fixed_amount" | "trader_wallet_ratio";
  fixed_trade_amount_override?: number | null;
  copy_wallet_mode:
    | "dynamic_main_wallet_percentage"
    | "fixed_snapshot_amount";
  copy_wallet_percentage: number;
  copy_wallet_fixed_amount?: number | null;
  created_at: string;
}

export interface FollowTraderRequest {
  max_position_size?: number | null;
  trader_alias?: string | null;
  sizing_mode?: "inherit_global" | "fixed_amount" | "trader_wallet_ratio";
  fixed_trade_amount_override?: number | null;
  copy_wallet_mode?:
    | "dynamic_main_wallet_percentage"
    | "fixed_snapshot_amount";
  copy_wallet_percentage?: number | null;
  copy_wallet_fixed_amount?: number | null;
}

export interface NotificationFollowRequest {
  feed_enabled?: boolean;
  email_enabled?: boolean;
}

export interface NotificationFollowedTrader {
  id: number;
  trader_wallet: string;
  is_active: boolean;
  feed_enabled: boolean;
  email_enabled: boolean;
  created_at: string;
}

export interface FollowingFeedEvent {
  id: number;
  trader_wallet: string;
  event_type: "opened" | "closed";
  market_id: string;
  token_id: string;
  side: string;
  size: number;
  price: number;
  prev_net_size: number;
  new_net_size: number;
  source_trade_history_id?: number | null;
  email_status: "pending" | "sent" | "failed" | "skipped";
  email_error?: string | null;
  emailed_at?: string | null;
  created_at: string;
}

// --- Copy Trade ---

export interface CopyTradeRecord {
  id: number;
  trader_wallet: string;
  market_id: string;
  side: string;
  size: number;
  price: number;
  status: string;
  pnl?: number | null;
  timestamp: string;
  source_trade_history_id?: number | null;
  trader_trade_notional?: number | null;
  trader_wallet_balance?: number | null;
  copy_wallet_base?: number | null;
  sizing_mode_applied?: string | null;
  copy_wallet_mode_applied?: string | null;
  calculation_warning?: string | null;
  calculation_details?: Record<string, unknown> | null;
}

export interface CopyEvaluationRow {
  source_trade_id: number;
  source_trade_id_ext?: string | null;
  source_timestamp?: string | null;
  market_id: string;
  side: string;
  price: number;
  source_trade_notional: number;
  copy_trade_id?: number | null;
  copy_timestamp?: string | null;
  copied_size?: number | null;
  copy_status: string;
  order_hash?: string | null;
  trader_wallet_balance?: number | null;
  copy_wallet_base?: number | null;
  ratio?: number | null;
  sizing_mode_applied?: string | null;
  copy_wallet_mode_applied?: string | null;
  warning?: string | null;
}

export interface CopyEvaluationResponse {
  wallet: string;
  rows: CopyEvaluationRow[];
  count: number;
  updated_at: string;
}

// --- Trade Execution ---

export interface ExecuteTradeRequest {
  token_id: string;
  market_id?: string;
  market_title?: string;
  side: "BUY" | "SELL";
  price: number;
  size: number;
  outcome?: string;
}

export interface ExecuteTradeResponse {
  success: boolean;
  order_hash?: string | null;
  trade_id?: number | null;
  status: string;
  side: string;
  size: number;
  price: number;
  error?: string | null;
}

// --- Service ---

export const tradesService = {
  async getTradeHistory(
    limit: number = 50,
    offset: number = 0,
  ): Promise<TradeHistoryResponse> {
    return apiClient.get<TradeHistoryResponse>(
      `/api/trades/history?limit=${limit}&offset=${offset}`,
    );
  },

  async getLeaderboard(
    limit: number = 25,
    period: string = "all_time",
  ): Promise<LeaderboardResponse> {
    return apiClient.get<LeaderboardResponse>(
      `/api/trades/leaderboard?limit=${limit}&period=${period}`,
      { timeout: 60_000 },
    );
  },

  async getTraderProfile(wallet: string): Promise<TraderProfile> {
    return apiClient.get<TraderProfile>(`/api/trades/trader/${wallet}`);
  },

  async followTrader(
    wallet: string,
    request?: number | FollowTraderRequest,
  ): Promise<FollowedTrader> {
    const body: FollowTraderRequest =
      typeof request === "number"
        ? { max_position_size: request }
        : request || {};
    return apiClient.post<FollowedTrader>(`/api/trades/follow/${wallet}`, {
      ...body,
    });
  },

  async unfollowTrader(wallet: string): Promise<void> {
    return apiClient.delete(`/api/trades/follow/${wallet}`);
  },

  async getFollowing(): Promise<FollowedTrader[]> {
    return apiClient.get<FollowedTrader[]>("/api/trades/following");
  },

  async followNotifications(
    wallet: string,
    request?: NotificationFollowRequest,
  ): Promise<NotificationFollowedTrader> {
    return apiClient.post<NotificationFollowedTrader>(
      `/api/trades/notification-follow/${wallet}`,
      request || {},
    );
  },

  async unfollowNotifications(wallet: string): Promise<void> {
    return apiClient.delete(`/api/trades/notification-follow/${wallet}`);
  },

  async getNotificationFollowing(): Promise<NotificationFollowedTrader[]> {
    return apiClient.get<NotificationFollowedTrader[]>(
      "/api/trades/notification-following",
    );
  },

  async getFollowingFeed(
    limit: number = 50,
    wallet?: string,
    event_type?: "opened" | "closed",
  ): Promise<FollowingFeedEvent[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    if (wallet) params.set("wallet", wallet);
    if (event_type) params.set("event_type", event_type);
    return apiClient.get<FollowingFeedEvent[]>(
      `/api/trades/following-feed?${params.toString()}`,
    );
  },

  async getCopyTrades(limit: number = 50): Promise<CopyTradeRecord[]> {
    return apiClient.get<CopyTradeRecord[]>(
      `/api/trades/copy-trades?limit=${limit}`,
    );
  },

  async getCopyTradePnl(): Promise<{ daily_pnl: number }> {
    return apiClient.get<{ daily_pnl: number }>("/api/trades/copy-trades/pnl");
  },

  async getCopyEvaluation(
    wallet: string,
    limit: number = 50,
  ): Promise<CopyEvaluationResponse> {
    return apiClient.get<CopyEvaluationResponse>(
      `/api/trades/copy-evaluation/${wallet}?limit=${limit}`,
    );
  },

  async executeTrade(
    request: ExecuteTradeRequest,
  ): Promise<ExecuteTradeResponse> {
    return apiClient.post<ExecuteTradeResponse>("/api/trades/execute", request);
  },

  async cashOut(request: ExecuteTradeRequest): Promise<ExecuteTradeResponse> {
    return apiClient.post<ExecuteTradeResponse>("/api/trades/cash-out", {
      ...request,
      side: "SELL",
    });
  },
};
