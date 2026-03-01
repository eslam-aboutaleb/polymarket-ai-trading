import { useState } from "react";
import { tradesService, ExecuteTradeResponse } from "../services/tradesService";
import { getApiErrorMessage } from "../utils/apiError";

export interface CashOutModalProps {
  position: {
    title?: string;
    market?: string;
    asset?: string;
    asset_id?: string;
    token_id?: string;
    condition_id?: string;
    conditionId?: string;
    outcome?: string;
    size: number;
    curPrice?: number;
    avgPrice?: number;
  };
  onClose: () => void;
  onSuccess?: () => void;
}

export default function CashOutModal({
  position,
  onClose,
  onSuccess,
}: CashOutModalProps) {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ExecuteTradeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const curPrice = Number(position.curPrice || 0);
  const avgPrice = Number(position.avgPrice || 0);
  const size = Number(position.size || 0);
  const proceeds = size * curPrice;
  const invested = size * avgPrice;
  const pnl = proceeds - invested;
  const pnlPct = invested > 0 ? (pnl / invested) * 100 : 0;
  const title =
    position.title || position.market || position.asset || "this position";

  const tokenId =
    position.asset_id ||
    position.token_id ||
    position.condition_id ||
    position.conditionId ||
    "";

  const handleConfirm = async () => {
    if (!tokenId) {
      setError("Missing token ID for this position");
      console.error("[CashOut] No tokenId — position:", position);
      return;
    }

    console.log("[CashOut] handleConfirm called", {
      tokenId,
      market_id: position.condition_id || position.conditionId || "",
      price: curPrice > 0 ? curPrice : 0.5,
      size,
    });

    setLoading(true);
    setError(null);
    try {
      const res = await tradesService.cashOut({
        token_id: tokenId,
        market_id: position.condition_id || position.conditionId || "",
        market_title: title,
        side: "SELL",
        price: curPrice > 0 ? curPrice : 0.5,
        size,
        outcome: position.outcome || "",
      });
      console.log("[CashOut] API response:", res);
      setResult(res);

      if (!res.success) {
        // Backend returned success=false — surface the error to the user
        setError(res.error || "Trade was not executed. Please try again.");
      } else if (onSuccess) {
        setTimeout(onSuccess, 1500);
      }
    } catch (err: unknown) {
      console.error("[CashOut] API error:", err);
      setError(getApiErrorMessage(err, "Cash-out failed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4"
      onClick={onClose}
    >
      <div
        className="surface-panel w-full max-w-md p-0 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="p-5 border-b border-[var(--line)]">
          <div className="flex justify-between items-start">
            <div className="flex-1 min-w-0 pr-4">
              <h2 className="text-lg font-bold text-white">Cash Out</h2>
              <p className="text-sm text-soft mt-1 truncate">{title}</p>
            </div>
            <button
              onClick={onClose}
              className="text-muted hover:text-white transition text-xl leading-none"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="p-5 space-y-4">
          {/* Summary card */}
          <div className="surface-soft p-4 rounded-lg space-y-3">
            {position.outcome && (
              <div className="flex justify-between items-center">
                <span className="text-muted text-sm">Outcome</span>
                <span className="chip chip-accent text-xs">
                  {position.outcome}
                </span>
              </div>
            )}
            <div className="flex justify-between">
              <span className="text-muted text-sm">Position Size</span>
              <span className="text-white mono text-sm">
                {size >= 1 ? size.toFixed(2) : size.toFixed(4)} shares
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted text-sm">Entry Price</span>
              <span className="text-soft mono text-sm">
                {(avgPrice * 100).toFixed(1)}¢
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted text-sm">Current Price</span>
              <span className="text-white mono text-sm">
                {(curPrice * 100).toFixed(1)}¢
              </span>
            </div>
            <div className="border-t border-[var(--line)] pt-3 flex justify-between">
              <span className="text-muted text-sm">Est. Proceeds</span>
              <span className="text-white font-semibold mono">
                ${proceeds.toFixed(2)}
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted text-sm">P&L</span>
              <span
                className={`font-semibold mono ${pnl >= 0 ? "status-good" : "status-bad"}`}
              >
                {pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} ({pnlPct >= 0 ? "+" : ""}
                {pnlPct.toFixed(1)}%)
              </span>
            </div>
          </div>

          {/* Warning */}
          <div className="p-3 rounded-lg bg-[var(--accent-soft)] border border-[var(--accent)]/30 text-sm text-[var(--accent-strong)]">
            This will sell your entire position at the current market price.
          </div>

          {/* Error */}
          {error && (
            <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* Success */}
          {result?.success && (
            <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-sm">
              <p className="font-semibold">Position closed!</p>
              <p className="text-xs mt-1 mono">
                Sold {size} shares @ {(curPrice * 100).toFixed(1)}¢
              </p>
              {result.order_hash && (
                <p className="text-xs mt-1 text-muted truncate">
                  Hash: {result.order_hash}
                </p>
              )}
            </div>
          )}

          {/* Buttons */}
          {!result?.success ? (
            <div className="flex gap-3">
              <button
                onClick={onClose}
                className="flex-1 btn-muted py-3 text-sm"
              >
                Cancel
              </button>
              <button
                onClick={handleConfirm}
                disabled={loading || !tokenId}
                className="flex-1 btn-success py-3 text-sm font-bold flex items-center justify-center gap-2"
              >
                {loading && (
                  <span className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full inline-block" />
                )}
                {loading ? "Selling..." : "Confirm Cash Out"}
              </button>
            </div>
          ) : (
            <button
              onClick={onClose}
              className="w-full btn-muted py-3 text-sm font-bold"
            >
              Done
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
