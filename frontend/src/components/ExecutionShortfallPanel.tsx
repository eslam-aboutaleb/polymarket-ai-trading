/**
 * Post-fill cost waterfall: modeled shortfall versus measured shortfall, both against
 * the same arrival midpoint. Only exchange-settled orders that match exactly one
 * estimate appear. Missing components (fees, missed-trade cost) are listed, not zeroed.
 *
 * @module components/ExecutionShortfallPanel
 */
import { useEffect, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import { QualityResponse, executionEstimateService } from "../services/executionEstimateService";

function n(value: string | null, digits = 4): string {
  if (value == null) return "not observed";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "not observed";
}

export default function ExecutionShortfallPanel() {
  const [data, setData] = useState<QualityResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    executionEstimateService
      .quality(30)
      .then((response) => !cancelled && setData(response))
      .catch((err) => !cancelled && setError(getApiErrorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <section className="surface-panel p-4 space-y-3" data-testid="shortfall-panel">
      <h3 className="text-white font-semibold">Implementation shortfall (estimate vs fill)</h3>
      {error && (
        <p className="text-xs text-soft">
          Shortfall quality is unavailable ({error}). It requires the execution estimator to be
          enabled.
        </p>
      )}
      {data && (
        <>
          <p className="text-xs text-soft">
            {data.summary.matched_settled} settled order(s) matched to an estimate;{" "}
            {data.summary.unmatched_estimates} estimate(s) unmatched,{" "}
            {data.summary.ambiguous_estimates} ambiguous. p50/p95 error appears only with{" "}
            {data.summary.percentile_min_sample}+ samples (p50: {n(data.summary.p50_error_bps, 1)},
            p95: {n(data.summary.p95_error_bps, 1)} bps). Not a profitability claim.
          </p>
          {data.rows.length === 0 ? (
            <p className="text-sm text-soft">
              No settled fills have been matched to an estimate yet.
            </p>
          ) : (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-soft">
                  <th>Trade</th>
                  <th>Arrival mid</th>
                  <th>Est. cost</th>
                  <th>Actual cost</th>
                  <th>Fee</th>
                  <th>Missed trade</th>
                  <th>Error (bps)</th>
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row) => (
                  <tr key={row.estimate_record_id} className="text-white mono">
                    <td>#{row.trade_id}</td>
                    <td>{n(row.arrival_mid)}</td>
                    <td>{n(row.waterfall.estimated_price_cost)}</td>
                    <td>{n(row.waterfall.actual_price_cost)}</td>
                    <td>{n(row.waterfall.fee)}</td>
                    <td>{n(row.waterfall.missed_trade_cost)}</td>
                    <td>{n(row.gap.error_bps, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="text-xs text-soft">{data.fees_note}</p>
        </>
      )}
    </section>
  );
}
