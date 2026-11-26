/**
 * Pre-trade cost preview for the trade drawer.
 *
 * Walks the captured, validated order book for the entered size and shows the
 * executable VWAP, fees and depth coverage. It is a read-only estimate: it never
 * submits, signs or cancels an order, and it expires with the captured quote.
 *
 * @module components/ExecutionCostPreview
 */
import { useEffect, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import { EstimateResponse, executionEstimateService } from "../services/executionEstimateService";

const CONDITION_ID = /^0x[0-9a-fA-F]{64}$/;
const DEBOUNCE_MS = 600;

interface Props {
  conditionId: string;
  outcome: "YES" | "NO";
  side: "BUY" | "SELL";
  shares: number;
}

function money(value: string | null, digits = 4): string {
  if (value == null) return "n/a";
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(digits) : "n/a";
}

export default function ExecutionCostPreview({ conditionId, outcome, side, shares }: Props) {
  const [feeBps, setFeeBps] = useState("");
  const [data, setData] = useState<EstimateResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const roundedShares = Math.floor(shares * 100) / 100;
  const eligible = CONDITION_ID.test(conditionId) && roundedShares >= 1 && feeBps !== "";
  const feeValid = feeBps !== "" && Number(feeBps) >= 0 && Number(feeBps) <= 10000;

  useEffect(() => {
    setData(null);
    setError(null);
    if (!eligible || !feeValid) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      setLoading(true);
      executionEstimateService
        .estimate({
          condition_id: conditionId,
          outcome,
          request: {
            order_intent_id: `ticket-${Date.now()}`,
            mode: "MARKETABLE_LIMIT",
            side,
            shares: roundedShares.toFixed(2),
            max_slippage_bps: "500",
            fee_bps: feeBps,
            fee_version: "user-entered-flat-bps",
          },
        })
        .then((response) => !cancelled && setData(response))
        .catch((err) => !cancelled && setError(getApiErrorMessage(err)))
        .finally(() => !cancelled && setLoading(false));
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [conditionId, outcome, side, roundedShares, feeBps, eligible, feeValid]);

  if (!CONDITION_ID.test(conditionId)) return null;
  const estimate = data?.estimate ?? null;
  const priced = estimate?.status === "modeled" && estimate.executable_vwap != null;

  return (
    <div
      className="surface-soft p-3.5 rounded-lg space-y-2 text-sm"
      data-testid="execution-cost-preview"
      aria-live="polite"
    >
      <div className="flex items-center justify-between gap-2">
        <span className="text-muted font-semibold">Pre-trade cost preview</span>
        <label className="flex items-center gap-1 text-xs text-soft">
          Taker fee (bps)
          <input
            aria-label="Taker fee assumption in basis points"
            className="w-16 bg-transparent border border-[var(--line)] rounded px-1 mono"
            inputMode="decimal"
            value={feeBps}
            onChange={(e) => setFeeBps(e.target.value.replace(/[^0-9.]/g, ""))}
          />
        </label>
      </div>
      {feeBps === "" && (
        <p className="text-xs text-soft">
          Enter a fee assumption to price fees. Fees are never assumed to be zero.
        </p>
      )}
      {loading && <p className="text-xs text-soft">Walking the captured order book...</p>}
      {error && <p className="text-xs text-red-300">{error}</p>}
      {estimate && !priced && (
        <p className="text-xs text-yellow-300" data-testid="preview-no-price">
          No price shown:{" "}
          {(estimate.reason ?? data?.reason ?? "unavailable").replace(/_/g, " ").toLowerCase()}
          {estimate.fill_status === "NONE" && estimate.status === "modeled"
            ? ". Displayed depth within your limit does not cover this order."
            : "."}
        </p>
      )}
      {!estimate && data && (
        <p className="text-xs text-yellow-300">
          No captured book for this market ({(data.reason ?? "unavailable").toLowerCase()}).
        </p>
      )}
      {priced && estimate && (
        <>
          <div className="flex justify-between">
            <span className="text-muted">Executable VWAP</span>
            <span className="text-white mono">{money(estimate.executable_vwap)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">Fees (assumed)</span>
            <span className="text-white mono">{money(estimate.fees)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">Walk vs touch</span>
            <span className="text-white mono">{money(estimate.expected_price_impact)}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-muted">Displayed depth covers</span>
            <span className="text-white mono">
              {(Number(estimate.depth_coverage) * 100).toFixed(0)}% ({estimate.fill_status})
            </span>
          </div>
          {estimate.fill_status === "PARTIAL" && (
            <p className="text-xs text-yellow-300">
              Only {estimate.fillable_quantity} of {estimate.quantity} shares are priced. The rest
              is not priced as a fill.
            </p>
          )}
          <p className="text-xs text-soft">
            Estimate from displayed depth, not a guaranteed fill. Expires{" "}
            {estimate.quote_expiry ? new Date(estimate.quote_expiry).toLocaleTimeString() : "n/a"};
            a changed quote voids it. Preview only: nothing is submitted.
          </p>
        </>
      )}
    </div>
  );
}
