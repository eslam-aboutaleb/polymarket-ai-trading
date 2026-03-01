import { useState, useEffect, useMemo } from "react";
import {
  tradesService,
  ExecuteTradeRequest,
  ExecuteTradeResponse,
} from "../services/tradesService";

// ── Types ──────────────────────────────────────────────────────────

export interface OutcomeToken {
  token_id: string;
  outcome: string; // "Yes" | "No"
  price: number; // 0-1  (e.g. 0.62 means 62¢)
}

export interface TradeModalMarket {
  /** Market slug or condition ID (record-keeping) */
  market_id: string;
  /** Human-readable question */
  title: string;
  /** Image URL (optional) */
  image?: string;
  /** Both outcome tokens */
  tokens: OutcomeToken[];
}

interface TradeModalProps {
  market: TradeModalMarket | null;
  onClose: () => void;
  onSuccess?: (result: ExecuteTradeResponse) => void;
}

// ── Helpers ─────────────────────────────────────────────────────────

const pct = (v: number) => `${(v * 100).toFixed(1)}¢`;
const fmt$ = (v: number) =>
  `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const MIN_BUY_USDC = 1;

// ════════════════════════════════════════════════════════════════════
// TradeModal — Polymarket-style Yes/No + Buy/Sell
// ════════════════════════════════════════════════════════════════════

export default function TradeModal({
  market,
  onClose,
  onSuccess,
}: TradeModalProps) {
  // ── State ──────────────────────────────────────────────────────
  const [outcome, setOutcome] = useState<"Yes" | "No">("Yes");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [amount, setAmount] = useState(""); // USDC amount
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ExecuteTradeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Reset on market change
  useEffect(() => {
    if (market) {
      setAmount("");
      setResult(null);
      setError(null);
    }
  }, [market]);

  // Clear result/error when switching outcome or side
  useEffect(() => {
    setResult(null);
    setError(null);
  }, [outcome, side]);

  // ── Derived values ─────────────────────────────────────────────
  const yesToken = useMemo(
    () => market?.tokens.find((t) => t.outcome === "Yes"),
    [market],
  );
  const noToken = useMemo(
    () => market?.tokens.find((t) => t.outcome === "No"),
    [market],
  );
  const activeToken = outcome === "Yes" ? yesToken : noToken;
  const activePrice = activeToken?.price ?? 0.5;

  const amountNum = parseFloat(amount) || 0;
  // When buying: you pay `amount` USDC and get `amount / price` shares
  // When selling: amount = number of shares to sell
  const shares =
    side === "BUY"
      ? activePrice > 0
        ? amountNum / activePrice
        : 0
      : amountNum;
  const cost = side === "BUY" ? amountNum : shares * activePrice;
  const potentialReturn = side === "BUY" ? shares * 1 : cost;
  const potentialProfit = side === "BUY" ? potentialReturn - cost : 0;

  const isValid =
    amountNum > 0 &&
    (side !== "BUY" || amountNum >= MIN_BUY_USDC) &&
    activeToken &&
    !loading;

  if (!market) return null;

  // ── Submit ─────────────────────────────────────────────────────
  const handleSubmit = async () => {
    if (!isValid || !activeToken) return;
    setLoading(true);
    setError(null);
    setResult(null);

    try {
      const req: ExecuteTradeRequest = {
        token_id: activeToken.token_id,
        market_id: market.market_id,
        market_title: market.title,
        side,
        price: activePrice,
        // BUY uses USDC amount; SELL uses share quantity
        size: amountNum,
        outcome: activeToken.outcome,
      };

      const resp = await tradesService.executeTrade(req);
      setResult(resp);
      if (resp.success && onSuccess) onSuccess(resp);
      if (!resp.success && resp.error) setError(resp.error);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Trade execution failed");
    } finally {
      setLoading(false);
    }
  };

  // ── Render ─────────────────────────────────────────────────────
  const yesPrice = yesToken?.price ?? 0.5;
  const noPrice = noToken?.price ?? 0.5;

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={onClose}
    >
      <div
        className="surface-panel w-full max-w-md overflow-hidden rounded-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* ── Header ─────────────────────────────────────────── */}
        <div className="p-5 border-b border-[var(--line)]">
          <div className="flex justify-between items-start gap-3">
            <h2 className="text-base font-bold text-white leading-snug flex-1 min-w-0">
              {market.title}
            </h2>
            <button
              onClick={onClose}
              className="text-muted hover:text-white transition text-xl leading-none shrink-0"
            >
              ✕
            </button>
          </div>
        </div>

        {/* ── Outcome Tabs (Yes / No) ────────────────────────── */}
        <div className="flex border-b border-[var(--line)]">
          <button
            onClick={() => setOutcome("Yes")}
            className={`flex-1 py-3 text-sm font-semibold transition relative ${
              outcome === "Yes"
                ? "text-emerald-400"
                : "text-muted hover:text-white"
            }`}
          >
            <span className="flex items-center justify-center gap-2">
              Yes
              <span
                className={`text-xs font-mono px-1.5 py-0.5 rounded ${
                  outcome === "Yes"
                    ? "bg-emerald-500/20 text-emerald-400"
                    : "bg-[var(--bg-soft)] text-muted"
                }`}
              >
                {pct(yesPrice)}
              </span>
            </span>
            {outcome === "Yes" && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-emerald-400" />
            )}
          </button>
          <button
            onClick={() => setOutcome("No")}
            className={`flex-1 py-3 text-sm font-semibold transition relative ${
              outcome === "No" ? "text-red-400" : "text-muted hover:text-white"
            }`}
          >
            <span className="flex items-center justify-center gap-2">
              No
              <span
                className={`text-xs font-mono px-1.5 py-0.5 rounded ${
                  outcome === "No"
                    ? "bg-red-500/20 text-red-400"
                    : "bg-[var(--bg-soft)] text-muted"
                }`}
              >
                {pct(noPrice)}
              </span>
            </span>
            {outcome === "No" && (
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-red-400" />
            )}
          </button>
        </div>

        {/* ── Buy / Sell Toggle ────────────────────────────────── */}
        <div className="px-5 pt-4">
          <div className="flex gap-2 p-1 rounded-lg bg-[var(--bg-soft)]">
            <button
              onClick={() => setSide("BUY")}
              className={`flex-1 py-2 rounded-md text-sm font-semibold transition ${
                side === "BUY"
                  ? "bg-emerald-500/20 text-emerald-400 shadow-sm"
                  : "text-muted hover:text-white"
              }`}
            >
              Buy
            </button>
            <button
              onClick={() => setSide("SELL")}
              className={`flex-1 py-2 rounded-md text-sm font-semibold transition ${
                side === "SELL"
                  ? "bg-red-500/20 text-red-400 shadow-sm"
                  : "text-muted hover:text-white"
              }`}
            >
              Sell
            </button>
          </div>
        </div>

        {/* ── Body ───────────────────────────────────────────── */}
        <div className="p-5 space-y-4">
          {/* Amount Input */}
          <div>
            <label className="block text-sm text-soft mb-1.5">
              {side === "BUY" ? "Amount (USDC)" : "Shares to Sell"}
            </label>
            <div className="relative">
              <span className="absolute left-3 top-1/2 -translate-y-1/2 text-muted text-sm">
                {side === "BUY" ? "$" : "#"}
              </span>
              <input
                type="number"
                min="0.01"
                step="any"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder={side === "BUY" ? "10.00" : "100"}
                className="w-full pl-7 pr-4 py-2.5 rounded-lg bg-[var(--bg-soft)] border border-[var(--line)] text-white placeholder-[var(--text-muted)] focus:outline-none focus:border-[var(--accent)] transition mono"
              />
            </div>
            {/* Quick-fill buttons */}
            <div className="flex gap-2 mt-2">
              {(side === "BUY" ? [5, 10, 25, 50, 100] : [10, 50, 100, 500]).map(
                (n) => (
                  <button
                    key={n}
                    onClick={() => setAmount(String(n))}
                    className="flex-1 py-1 text-xs rounded bg-[var(--bg-soft)] text-muted hover:text-white hover:bg-[var(--bg-card)] border border-[var(--line)] transition"
                  >
                    {side === "BUY" ? `$${n}` : n}
                  </button>
                ),
              )}
            </div>
            {side === "BUY" && (
              <p className="text-xs text-muted mt-2">
                Minimum marketable buy is {fmt$(MIN_BUY_USDC)}.
              </p>
            )}
          </div>

          {/* Price display */}
          <div className="flex items-center justify-between text-sm">
            <span className="text-muted">{outcome} Price</span>
            <span className="text-white font-mono font-semibold">
              {pct(activePrice)}
            </span>
          </div>

          {/* Order Summary */}
          {amountNum > 0 && (
            <div className="surface-soft p-3.5 rounded-lg space-y-2 text-sm">
              {side === "BUY" ? (
                <>
                  <div className="flex justify-between">
                    <span className="text-muted">You pay</span>
                    <span className="text-white mono">{fmt$(cost)}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Est. shares</span>
                    <span className="text-white mono">
                      {shares.toFixed(2)} {outcome}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Payout if {outcome}</span>
                    <span className="text-white mono">
                      {fmt$(potentialReturn)}
                    </span>
                  </div>
                  <div className="border-t border-[var(--line)] pt-2 flex justify-between font-semibold">
                    <span className="text-muted">Potential profit</span>
                    <span className="status-good mono">
                      +{fmt$(potentialProfit)} (
                      {((potentialProfit / cost) * 100).toFixed(0)}%)
                    </span>
                  </div>
                </>
              ) : (
                <>
                  <div className="flex justify-between">
                    <span className="text-muted">Shares to sell</span>
                    <span className="text-white mono">
                      {amountNum.toFixed(2)} {outcome}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted">Est. return</span>
                    <span className="text-white mono">{fmt$(cost)}</span>
                  </div>
                </>
              )}
            </div>
          )}

          {/* Error */}
          {error && (
            <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* Success */}
          {result?.success && (
            <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-sm">
              <p className="font-semibold">
                {side === "BUY" ? "Bought" : "Sold"} {outcome} shares!
              </p>
              <p className="text-xs mt-1 mono">
                {result.side}{" "}
                {result.side === "BUY"
                  ? `${fmt$(result.size ?? amountNum)}`
                  : `${result.size?.toFixed?.(2) ?? result.size} shares`}{" "}
                @ {pct(result.price)}
              </p>
              {result.order_hash && (
                <p className="text-xs mt-1 text-muted truncate">
                  Hash: {result.order_hash}
                </p>
              )}
            </div>
          )}

          {/* Submit */}
          {!result?.success && (
            <button
              onClick={handleSubmit}
              disabled={!isValid}
              className={`w-full py-3 rounded-lg font-bold text-sm transition ${
                side === "BUY" ? "btn-success" : "btn-danger"
              }`}
            >
              {loading ? (
                <span className="flex items-center justify-center gap-2">
                  <svg
                    className="animate-spin h-4 w-4"
                    viewBox="0 0 24 24"
                    fill="none"
                  >
                    <circle
                      cx="12"
                      cy="12"
                      r="10"
                      stroke="currentColor"
                      strokeWidth="3"
                      className="opacity-30"
                    />
                    <path
                      d="M4 12a8 8 0 018-8"
                      stroke="currentColor"
                      strokeWidth="3"
                      strokeLinecap="round"
                    />
                  </svg>
                  Placing order…
                </span>
              ) : (
                <>
                  {side === "BUY" ? "Buy" : "Sell"} {outcome}{" "}
                  {amountNum > 0 &&
                    (side === "BUY"
                      ? `— ${fmt$(amountNum)}`
                      : `— ${amountNum} shares`)}
                </>
              )}
            </button>
          )}

          {/* Close after success */}
          {result?.success && (
            <button
              onClick={onClose}
              className="w-full py-3 rounded-lg font-bold text-sm btn-muted"
            >
              Done
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
