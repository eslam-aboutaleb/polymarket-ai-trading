/**
 * Shared types for the trade ticket, its validation, and recently-traded markets.
 *
 * `TradePreferences` is persisted per tab by `utils/tradePreferences.ts`; `TradeValidationResult` is
 * the shape the ticket's validate step returns (errors, warnings, and whether a high-notional
 * confirmation is required); `RecentTradeMarket` is the snapshot stored by
 * `utils/tradingWorkspace.ts`.
 *
 * @module types/trading
 */

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
