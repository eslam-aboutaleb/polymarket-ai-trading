export type TradeTicketSide = "BUY" | "SELL";
export type TradeTicketOutcome = "Yes" | "No";
export type TradeTicketSource = "markets" | "opportunities" | "dashboard";
export type TradePresetMode = "fixed" | "percentage";

export interface TradeTicketToken {
  token_id: string;
  outcome: string;
  price: number;
}

export interface TradePreferences {
  lastSide: TradeTicketSide;
  lastOutcome: TradeTicketOutcome;
  lastAmount: string;
  highNotionalConfirmThreshold: number;
  preferredPresetMode: TradePresetMode;
}

export interface TradeValidationResult {
  errors: string[];
  warnings: string[];
  requiresConfirm: boolean;
}

export interface RecentTradeMarket {
  market_id: string;
  title: string;
  image?: string;
  watch_key?: string;
  tokens: TradeTicketToken[];
  bestAsk?: number | string | null;
  bestBid?: number | string | null;
  liquidity?: number;
  quote_timestamp?: string;
  source: TradeTicketSource;
  last_traded_at: string;
}
