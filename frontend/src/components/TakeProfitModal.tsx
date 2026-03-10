import { useState, useEffect, useCallback } from "react";
import {
  takeProfitService,
  TakeProfitOrder,
} from "../services/takeProfitService";
import { getApiErrorMessage } from "../utils/apiError";

export interface TakeProfitModalProps {
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
  /** Existing take-profit for this position (if any) */
  existingTakeProfit?: TakeProfitOrder | null;
  onClose: () => void;
  onSuccess?: () => void;
}

export default function TakeProfitModal({
  position,
  existingTakeProfit,
  onClose,
  onSuccess,
}: TakeProfitModalProps) {
  const curPrice = Number(position.curPrice || 0);
  const avgPrice = Number(position.avgPrice || 0);
  const size = Number(position.size || 0);
  const title =
    position.title || position.market || position.asset || "this position";

  const tokenId =
    position.asset_id ||
    position.token_id ||
    position.condition_id ||
    position.conditionId ||
    "";

  // Default take-profit price: 10¢ above current, clamped 2-99
  const defaultTarget = Math.max(
    2,
    Math.min(99, Math.round(curPrice * 100 + 10)),
  );

  const [targetCents, setTargetCents] = useState<number>(
    existingTakeProfit
      ? Math.round(existingTakeProfit.take_profit_price * 100)
      : defaultTarget,
  );
  const [loading, setLoading] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [success, setSuccess] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Derived values
  const targetDecimal = targetCents / 100;
  const gainPerShare = targetDecimal - curPrice;
  const totalGain = gainPerShare * size;
  const gainPct = curPrice > 0 ? (gainPerShare / curPrice) * 100 : 0;

  // Slider change
  const handleSlider = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setTargetCents(Number(e.target.value));
  }, []);

  const handleInputChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const v = parseFloat(e.target.value);
      if (!isNaN(v) && v >= 0 && v <= 100) {
        setTargetCents(Math.round(v * 10) / 10);
      }
    },
    [],
  );

  // Place or update take-profit
  const handleConfirm = async () => {
    if (!tokenId) {
      setError("Missing token ID for this position");
      return;
    }
    if (targetCents <= 0 || targetCents >= 100) {
      setError("Target price must be between 1¢ and 99¢");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await takeProfitService.setTakeProfit({
        token_id: tokenId,
        market_id: position.condition_id || position.conditionId || "",
        market_title: title,
        outcome: position.outcome || "",
        size,
        take_profit_price: targetDecimal,
      });
      setSuccess(
        existingTakeProfit
          ? `Take profit updated to ${targetCents}¢`
          : `Take profit set at ${targetCents}¢ — monitoring in real-time`,
      );
      if (onSuccess) setTimeout(onSuccess, 1200);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to set take profit"));
    } finally {
      setLoading(false);
    }
  };

  // Remove existing take-profit
  const handleRemove = async () => {
    if (!existingTakeProfit) return;
    setRemoving(true);
    setError(null);
    try {
      await takeProfitService.cancelTakeProfit(existingTakeProfit.id);
      setSuccess("Take profit removed");
      if (onSuccess) setTimeout(onSuccess, 1000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, "Failed to remove take profit"));
    } finally {
      setRemoving(false);
    }
  };

  // Keyboard support: ESC closes
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

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
              <h2 className="text-lg font-bold text-white">
                {existingTakeProfit ? "Edit Take Profit" : "Set Take Profit"}
              </h2>
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
        <div className="p-5 space-y-5">
          {/* Position Info */}
          <div className="surface-soft p-4 rounded-lg space-y-2">
            {position.outcome && (
              <div className="flex justify-between items-center">
                <span className="text-muted text-sm">Outcome</span>
                <span className="chip chip-accent text-xs">
                  {position.outcome}
                </span>
              </div>
            )}
            <div className="flex justify-between">
              <span className="text-muted text-sm">Size</span>
              <span className="text-white mono text-sm">
                {size >= 1 ? size.toFixed(2) : size.toFixed(4)} shares
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted text-sm">Entry</span>
              <span className="text-soft mono text-sm">
                {(avgPrice * 100).toFixed(1)}¢
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted text-sm">Current</span>
              <span className="text-white mono text-sm">
                {(curPrice * 100).toFixed(1)}¢
              </span>
            </div>
          </div>

          {/* Target Price Slider */}
          <div>
            <label className="block text-sm text-soft mb-2">
              Take Profit Price
            </label>
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={Math.min(99, Math.round(curPrice * 100) + 1)}
                max={99}
                step={1}
                value={targetCents}
                onChange={handleSlider}
                className="flex-1 accent-emerald-500 h-2 rounded-full appearance-none bg-[var(--bg-soft)] cursor-pointer"
              />
              <div className="relative w-20">
                <input
                  type="number"
                  min={0.1}
                  max={99.9}
                  step={0.1}
                  value={targetCents}
                  onChange={handleInputChange}
                  className="w-full px-3 py-2 rounded-lg bg-[var(--bg-soft)] border border-[var(--line)] text-white text-center mono text-sm focus:outline-none focus:border-emerald-500 transition"
                />
                <span className="absolute right-2 top-1/2 -translate-y-1/2 text-muted text-xs">
                  ¢
                </span>
              </div>
            </div>

            {/* Visual scale */}
            <div className="flex justify-between mt-1 text-xs text-muted">
              <span>Current: {(curPrice * 100).toFixed(1)}¢</span>
              <span>99¢</span>
            </div>
          </div>

          {/* Impact summary */}
          <div className="surface-soft p-4 rounded-lg space-y-2">
            <div className="flex justify-between">
              <span className="text-muted text-sm">Trigger Price</span>
              <span className="text-white font-semibold mono">
                {targetCents.toFixed(0)}¢
              </span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted text-sm">Distance from Current</span>
              <span className="status-good mono text-sm">
                +{(gainPerShare * 100).toFixed(1)}¢ ({gainPct.toFixed(1)}%)
              </span>
            </div>
            <div className="border-t border-[var(--line)] pt-2 flex justify-between">
              <span className="text-muted text-sm">
                Estimated Profit if Triggered
              </span>
              <span className="status-good font-semibold mono">
                +${Math.abs(totalGain).toFixed(2)}
              </span>
            </div>
          </div>

          {/* How it works */}
          <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-xs flex items-start gap-2">
            <span className="text-base leading-none mt-0.5">📈</span>
            <span>
              Prices are monitored every 10 seconds. When the price rises to or
              above your target price, a sell order is automatically placed to
              lock in your profits.
            </span>
          </div>

          {/* Error */}
          {error && (
            <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* Success */}
          {success && (
            <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-sm font-medium">
              {success}
            </div>
          )}

          {/* Buttons */}
          {!success ? (
            <div className="space-y-2">
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
                  {loading
                    ? "Setting..."
                    : existingTakeProfit
                      ? "Update Take Profit"
                      : "Set Take Profit"}
                </button>
              </div>
              {existingTakeProfit && (
                <button
                  onClick={handleRemove}
                  disabled={removing}
                  className="w-full py-2 text-sm text-muted hover:text-red-400 transition flex items-center justify-center gap-1.5"
                >
                  {removing && (
                    <span className="animate-spin h-3 w-3 border-2 border-current border-t-transparent rounded-full inline-block" />
                  )}
                  Remove Take Profit
                </button>
              )}
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
