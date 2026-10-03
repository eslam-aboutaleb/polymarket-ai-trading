/**
 * Shared UI for the stop-loss and take-profit trigger modals.
 *
 * Both features are the same workflow with opposite direction: pick a trigger
 * price on a slider, see the P&L impact, save it, and optionally remove an
 * existing order. `StopLossModal` and `TakeProfitModal` previously duplicated
 * the whole 300-line component, which meant every UX or validation fix had to
 * be written twice. This component owns the single implementation; the two
 * modals supply only their direction-specific copy, bounds and service calls.
 */

import { useCallback, useEffect, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import { getTradingKeyStatus } from "../services/tradingKeyService";
import TradingKeyPrompt from "./TradingKeyPrompt";

/** The position fields the trigger modals read. */
export interface TriggerPosition {
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
}

/** Fields common to the stop-loss and take-profit endpoints. */
export interface TriggerRequestBase {
  token_id: string;
  market_id: string;
  market_title: string;
  outcome: string;
  size: number;
}

/**
 * Payload sent to the stop-loss / take-profit endpoints.
 *
 * The trigger price is named differently per feature, so this is a union of
 * the two shapes; {@link TriggerConfig.priceField} says which one is used.
 */
export type TriggerRequest =
  | (TriggerRequestBase & { stop_price: number })
  | (TriggerRequestBase & { take_profit_price: number });

/** Server calls the modal needs, injected by the feature-specific wrapper. */
export interface TriggerService {
  /** Create or replace the resting order. */
  setTrigger: (request: TriggerRequest) => Promise<unknown>;
  /** Cancel an existing resting order by id. */
  cancelTrigger: (orderId: number) => Promise<unknown>;
}

/**
 * Direction-specific presentation and bounds for a trigger modal.
 *
 * `direction` is `1` when a trigger is hit by a price rise (take profit) and
 * `-1` when hit by a price fall (stop loss); it drives the sign shown in the
 * impact summary.
 */
export interface TriggerConfig {
  /** Human-readable feature name, e.g. "Stop Loss". */
  label: string;
  /** Name of the price field on the API request. */
  priceField: "stop_price" | "take_profit_price";
  /** `+1` for profit-taking, `-1` for loss-cutting. */
  direction: 1 | -1;
  /** Default trigger offset from the current price, in cents. */
  defaultOffsetCents: number;
  /** Lowest selectable trigger price, in cents. */
  minCents: number;
  /** Highest selectable trigger price, in cents. */
  maxCents: number;
  /** Endpoint adapter. */
  service: TriggerService;
  /** Slider/number accent class. */
  accentClass: string;
  /** P&L summary colour class. */
  statusClass: string;
  /** Tailwind gradient classes for the primary action button. */
  actionClass: string;
  /** Tailwind gradient classes for the "how it works" callout. */
  calloutClass: string;
  /** Icon shown in the callout. */
  calloutIcon: string;
  /** Explanatory sentence under the slider. */
  howItWorks: string;
  /** Label for the impact total, e.g. "Max Loss if Triggered". */
  impactLabel: string;
}

export interface PriceTriggerModalProps {
  position: TriggerPosition;
  /** Existing resting order for this position, when one is already set. */
  existingOrder?: { id: number } | null;
  /** The already-known trigger price, in decimal form. */
  existingPrice?: number | null;
  config: TriggerConfig;
  /**
   * When true, creating/updating the order requires a stored
   * trading key (auto-trading strategies sign orders with it).
   */
  requireTradingKey?: boolean;
  onClose: () => void;
  onSuccess?: () => void;
}

export default function PriceTriggerModal({
  position,
  existingOrder,
  existingPrice,
  config,
  requireTradingKey,
  onClose,
  onSuccess,
}: PriceTriggerModalProps) {
  const curPrice = Number(position.curPrice || 0);
  const avgPrice = Number(position.avgPrice || 0);
  const size = Number(position.size || 0);
  const title = position.title || position.market || position.asset || "this position";

  const tokenId =
    position.asset_id || position.token_id || position.condition_id || position.conditionId || "";

  // Default trigger: `defaultOffsetCents` away from the current price, clamped
  // to the feature's own bounds.
  const defaultCents = Math.max(
    config.minCents,
    Math.min(config.maxCents, Math.round(curPrice * 100 + config.defaultOffsetCents)),
  );

  const [cents, setCents] = useState<number>(
    existingOrder && existingPrice != null ? Math.round(existingPrice * 100) : defaultCents,
  );
  const [loading, setLoading] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [success, setSuccess] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tradingKeyRequired, setTradingKeyRequired] = useState(false);

  // Signed impact of the trigger, in dollars and percent of the current price.
  const triggerDecimal = cents / 100;
  const perShare = (triggerDecimal - curPrice) * config.direction;
  const total = perShare * size;
  const pct = curPrice > 0 ? (perShare / curPrice) * 100 : 0;

  const handleSlider = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setCents(Number(e.target.value));
  }, []);

  const handleInputChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const value = parseFloat(e.target.value);
    if (!Number.isNaN(value) && value >= 0 && value <= 100) {
      // One decimal place of precision.
      setCents(Math.round(value * 10) / 10);
    }
  }, []);

  const handleConfirm = async () => {
    if (!tokenId) {
      setError("Missing token ID for this position");
      return;
    }
    if (cents <= 0 || cents >= 100) {
      setError(`${config.label} price must be between 1¢ and 99¢`);
      return;
    }
    if (requireTradingKey) {
      let hasTradingKey = false;
      try {
        const status = await getTradingKeyStatus();
        hasTradingKey = status.has_trading_key;
      } catch {
        hasTradingKey = false;
      }
      if (!hasTradingKey) {
        setTradingKeyRequired(true);
        return;
      }
    }
    setLoading(true);
    setError(null);
    try {
      await config.service.setTrigger({
        token_id: tokenId,
        market_id: position.condition_id || position.conditionId || "",
        market_title: title,
        outcome: position.outcome || "",
        size,
        [config.priceField]: triggerDecimal,
      } as unknown as TriggerRequest);
      setSuccess(
        existingOrder
          ? `${config.label} updated to ${cents}¢`
          : `${config.label} set at ${cents}¢ — monitoring in real-time`,
      );
      if (onSuccess) setTimeout(onSuccess, 1200);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, `Failed to set ${config.label.toLowerCase()}`));
    } finally {
      setLoading(false);
    }
  };

  const handleRemove = async () => {
    if (!existingOrder) return;
    setRemoving(true);
    setError(null);
    try {
      await config.service.cancelTrigger(existingOrder.id);
      setSuccess(`${config.label} removed`);
      if (onSuccess) setTimeout(onSuccess, 1000);
    } catch (err: unknown) {
      setError(getApiErrorMessage(err, `Failed to remove ${config.label.toLowerCase()}`));
    } finally {
      setRemoving(false);
    }
  };

  // Escape closes the modal.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // The slider only spans the reachable side of the current price.
  const currentCents = Math.round(curPrice * 100);
  const sliderMin =
    config.direction === 1 ? Math.min(config.maxCents, currentCents + 1) : config.minCents;
  const sliderMax =
    config.direction === 1 ? config.maxCents : Math.max(config.minCents, currentCents - 1);
  const sign = config.direction === 1 ? "+" : "−";

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
                {existingOrder ? `Edit ${config.label}` : `Set ${config.label}`}
              </h2>
              <p className="text-sm text-soft mt-1 truncate">{title}</p>
            </div>
            <button
              onClick={onClose}
              aria-label="Close"
              className="text-muted hover:text-white transition text-xl leading-none"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Body */}
        <div className="p-5 space-y-5">
          {/* Position summary */}
          <div className="surface-soft p-4 rounded-lg space-y-2">
            {position.outcome && (
              <div className="flex justify-between items-center">
                <span className="text-muted text-sm">Outcome</span>
                <span className="chip chip-accent text-xs">{position.outcome}</span>
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
              <span className="text-soft mono text-sm">{(avgPrice * 100).toFixed(1)}¢</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted text-sm">Current</span>
              <span className="text-white mono text-sm">{(curPrice * 100).toFixed(1)}¢</span>
            </div>
          </div>

          {/* Trigger price slider */}
          <div>
            <label className="block text-sm text-soft mb-2">{config.label} Price</label>
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={sliderMin}
                max={sliderMax}
                step={1}
                value={cents}
                onChange={handleSlider}
                className={`flex-1 ${config.accentClass} h-2 rounded-full appearance-none bg-[var(--bg-soft)] cursor-pointer`}
              />
              <div className="relative w-20">
                <input
                  type="number"
                  min={0.1}
                  max={99.9}
                  step={0.1}
                  value={cents}
                  onChange={handleInputChange}
                  className={`w-full px-3 py-2 rounded-lg bg-[var(--bg-soft)] border border-[var(--line)] text-white text-center mono text-sm focus:outline-none transition ${config.accentClass}`}
                />
                <span className="absolute right-2 top-1/2 -translate-y-1/2 text-muted text-xs">
                  ¢
                </span>
              </div>
            </div>

            {/* Visual scale */}
            <div className="flex justify-between mt-1 text-xs text-muted">
              <span>
                {config.direction === 1 ? `Current: ${(curPrice * 100).toFixed(1)}¢` : "1¢"}
              </span>
              <span>
                {config.direction === 1 ? "99¢" : `Current: ${(curPrice * 100).toFixed(1)}¢`}
              </span>
            </div>
          </div>

          {/* Impact summary */}
          <div className="surface-soft p-4 rounded-lg space-y-2">
            <div className="flex justify-between">
              <span className="text-muted text-sm">Trigger Price</span>
              <span className="text-white font-semibold mono">{cents.toFixed(0)}¢</span>
            </div>
            <div className="flex justify-between">
              <span className="text-muted text-sm">Distance from Current</span>
              <span className={`${config.statusClass} mono text-sm`}>
                {sign}
                {(Math.abs(perShare) * 100).toFixed(1)}¢ ({pct.toFixed(1)}%)
              </span>
            </div>
            <div className="border-t border-[var(--line)] pt-2 flex justify-between">
              <span className="text-muted text-sm">{config.impactLabel}</span>
              <span className={`${config.statusClass} font-semibold mono`}>
                {sign}${Math.abs(total).toFixed(2)}
              </span>
            </div>
          </div>

          {/* How it works */}
          <div
            className={`p-3 rounded-lg border text-xs flex items-start gap-2 ${config.calloutClass}`}
          >
            <span className="text-base leading-none mt-0.5">{config.calloutIcon}</span>
            <span>{config.howItWorks}</span>
          </div>

          {/* Error */}
          {error && (
            <div className="p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* Trading key required */}
          {tradingKeyRequired && <TradingKeyPrompt strategy={config.label} />}

          {/* Success */}
          {success && (
            <div className="p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30 text-emerald-400 text-sm font-medium">
              {success}
            </div>
          )}

          {/* Actions */}
          {!success ? (
            <div className="space-y-2">
              <div className="flex gap-3">
                <button onClick={onClose} className="flex-1 btn-muted py-3 text-sm">
                  Cancel
                </button>
                <button
                  onClick={handleConfirm}
                  disabled={loading || !tokenId}
                  className={`flex-1 py-3 text-sm font-bold flex items-center justify-center gap-2 ${config.actionClass}`}
                >
                  {loading && (
                    <span className="animate-spin h-4 w-4 border-2 border-current border-t-transparent rounded-full inline-block" />
                  )}
                  {loading
                    ? "Setting..."
                    : existingOrder
                      ? `Update ${config.label}`
                      : `Set ${config.label}`}
                </button>
              </div>
              {existingOrder && (
                <button
                  onClick={handleRemove}
                  disabled={removing}
                  className="w-full py-2 text-sm text-muted hover:text-red-400 transition flex items-center justify-center gap-1.5"
                >
                  {removing && (
                    <span className="animate-spin h-3 w-3 border-2 border-current border-t-transparent rounded-full inline-block" />
                  )}
                  Remove {config.label}
                </button>
              )}
            </div>
          ) : (
            <button onClick={onClose} className="w-full btn-muted py-3 text-sm font-bold">
              Done
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
