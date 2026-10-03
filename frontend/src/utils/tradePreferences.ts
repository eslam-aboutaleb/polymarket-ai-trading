/**
 * Per-tab persistence for trade-ticket preferences (last side, outcome, amount, preset mode).
 *
 * Stored in `sessionStorage` rather than localStorage so preferences do not follow the user across
 * sessions. Every read is defensive: unknown values fall back to the defaults, and
 * `highNotionalConfirmThreshold` is coerced to a finite number >= 1 rounded to two decimals.
 *
 * @module utils/tradePreferences
 */

import {
  TradePreferences,
  TradePresetMode,
  TradeTicketOutcome,
  TradeTicketSide,
} from "../types/trading";

export const TRADE_PREFERENCES_KEY = "pm:trade-preferences:v1";

const DEFAULT_PREFERENCES: TradePreferences = {
  lastSide: "BUY",
  lastOutcome: "Yes",
  lastAmount: "",
  highNotionalConfirmThreshold: 250,
  preferredPresetMode: "fixed",
};

const normalizeSide = (value: unknown): TradeTicketSide => (value === "SELL" ? "SELL" : "BUY");

const normalizeOutcome = (value: unknown): TradeTicketOutcome => (value === "No" ? "No" : "Yes");

const normalizePresetMode = (value: unknown): TradePresetMode =>
  value === "percentage" ? "percentage" : "fixed";

const normalizeThreshold = (value: unknown): number => {
  const num = Number(value);
  if (!Number.isFinite(num) || num < 1) return DEFAULT_PREFERENCES.highNotionalConfirmThreshold;
  return Math.round(num * 100) / 100;
};

export function loadTradePreferences(): TradePreferences {
  if (typeof window === "undefined") return DEFAULT_PREFERENCES;
  try {
    const raw = sessionStorage.getItem(TRADE_PREFERENCES_KEY);
    if (!raw) return DEFAULT_PREFERENCES;
    const parsed = JSON.parse(raw) as Partial<TradePreferences>;
    return {
      lastSide: normalizeSide(parsed.lastSide),
      lastOutcome: normalizeOutcome(parsed.lastOutcome),
      lastAmount:
        typeof parsed.lastAmount === "string" ? parsed.lastAmount : DEFAULT_PREFERENCES.lastAmount,
      highNotionalConfirmThreshold: normalizeThreshold(parsed.highNotionalConfirmThreshold),
      preferredPresetMode: normalizePresetMode(parsed.preferredPresetMode),
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}

export function saveTradePreferences(update: Partial<TradePreferences>): TradePreferences {
  const next = {
    ...loadTradePreferences(),
    ...update,
  };
  if (typeof window !== "undefined") {
    sessionStorage.setItem(TRADE_PREFERENCES_KEY, JSON.stringify(next));
  }
  return next;
}

export function getDefaultTradePreferences(): TradePreferences {
  return DEFAULT_PREFERENCES;
}
