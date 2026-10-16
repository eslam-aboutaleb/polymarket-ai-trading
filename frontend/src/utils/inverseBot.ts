/**
 * Pure helpers behind the inverse-position-bot UI (money path).
 *
 * Dashboard and TradeHistory both render per-position inverse-bot controls, and the
 * position lookup, status chip class and P&L math they run on every price tick used
 * to be copied into each component — which is how they drifted apart. They live here
 * so both surfaces share one implementation.
 *
 * @module utils/inverseBot
 */

import { PolymarketPosition } from "../services/portfolioService";

/** Loose position shape: portfolio rows arrive with mixed snake/camelCase keys. */
export interface PositionRef {
  asset_id?: string;
  token_id?: string;
  condition_id?: string;
  conditionId?: string;
  title?: string;
  market?: string;
  outcome?: string;
}

/** Minimal stop-loss row shape (matches `StopLossOrder`). */
export interface StopLossRef {
  token_id: string;
}

/** Minimal take-profit row shape (matches `TakeProfitOrder`). */
export interface TakeProfitRef {
  token_id: string;
}

/** Row totals recomputed from live position prices. */
export interface PositionTotals {
  totalInvested: number;
  totalValue: number;
  totalPnl: number;
  pnlPercentage: number;
  activePositions: number;
}

/** Token id the inverse bot keys its rows on. */
export function inverseBotTokenId(pos: PositionRef): string {
  return String(pos.asset_id || pos.token_id || "");
}

/** Token id stop-loss / take-profit rows are matched on (falls back to condition id). */
export function riskOrderTokenId(pos: PositionRef): string {
  return pos.asset_id || pos.token_id || pos.condition_id || "";
}

/** Find the inverse-bot row tracking a position, if any. */
export function findInverseBotForPosition<T extends { token_id: string }>(
  rows: T[],
  pos: PositionRef,
): T | undefined {
  const tokenId = inverseBotTokenId(pos);
  return rows.find((row) => row.token_id === tokenId);
}

/** Find the active stop-loss order guarding a position, if any. */
export function findStopLossForPosition<T extends StopLossRef>(
  orders: T[],
  pos: PositionRef,
): T | undefined {
  const tokenId = riskOrderTokenId(pos);
  return orders.find((order) => order.token_id === tokenId);
}

/** Find the active take-profit order guarding a position, if any. */
export function findTakeProfitForPosition<T extends TakeProfitRef>(
  orders: T[],
  pos: PositionRef,
): T | undefined {
  const tokenId = riskOrderTokenId(pos);
  return orders.find((order) => order.token_id === tokenId);
}

/** Chip class for an inverse-bot status (inactive/active read as success). */
export function inverseStatusClass(status: string | null | undefined): string {
  if (status === "cooldown") return "chip-warning";
  if (status === "sell_only") return "chip-danger";
  if (status === "error") return "chip-danger";
  return "chip-success";
}

/** Recompute one position's P&L from a fresh price (4dp, as the UI expects). */
export function applyPositionPrice<T extends PolymarketPosition>(pos: T, newPrice: number): T {
  const size = Number(pos.size || 0);
  const avgPrice = Number(pos.avgPrice || 0);
  const invested = size * avgPrice;
  const value = size * newPrice;
  return {
    ...pos,
    curPrice: newPrice,
    pnl: +(value - invested).toFixed(4),
  } as T;
}

/** Recompute the summary totals the stat cards read from the position list. */
export function summarizePositions(positions: PolymarketPosition[]): PositionTotals {
  let totalInvested = 0;
  let totalValue = 0;
  let active = 0;
  for (const p of positions) {
    const s = Number(p.size || 0);
    const ap = Number(p.avgPrice || 0);
    const cp = Number(p.curPrice || ap);
    totalInvested += s * ap;
    totalValue += s * cp;
    if (s > 0) active++;
  }
  const totalPnl = totalValue - totalInvested;
  const pnlPct = totalInvested > 0 ? (totalPnl / totalInvested) * 100 : 0;
  return {
    totalInvested,
    totalValue,
    totalPnl,
    pnlPercentage: pnlPct,
    activePositions: active,
  };
}
