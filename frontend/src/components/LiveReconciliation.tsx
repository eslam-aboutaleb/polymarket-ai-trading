import { useEffect, useRef, useState } from "react";
import {
  executionAnalyticsService,
  LiveBalanceReference,
  LiveReconciliationReport,
} from "../services/executionAnalyticsService";
import { getApiErrorMessage } from "../utils/apiError";
import { isApiError } from "../utils/apiError";

const money = (value: string | null) =>
  value == null ? "Unknown" : `$${Number(value).toFixed(6)}`;

export default function LiveReconciliation() {
  const [snapshots, setSnapshots] = useState<LiveBalanceReference[]>([]);
  const [opening, setOpening] = useState("");
  const [closing, setClosing] = useState("");
  const [report, setReport] = useState<LiveReconciliationReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [available, setAvailable] = useState(true);
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  const request = useRef(0);
  useEffect(() => {
    alive.current = true;
    const sequence = ++request.current;
    void executionAnalyticsService
      .getBalanceSnapshots()
      .then((rows) => {
        if (!alive.current || sequence !== request.current) return;
        setSnapshots(rows);
        setOpening(rows[rows.length - 1]?.id ?? "");
        setClosing(rows[0]?.id ?? "");
      })
      .catch((cause) => {
        if (!alive.current || sequence !== request.current) return;
        if (isApiError(cause) && cause.status === 503) setAvailable(false);
        else setError(getApiErrorMessage(cause));
      });
    return () => {
      alive.current = false;
      request.current += 1;
    };
  }, []);

  async function capture() {
    const sequence = ++request.current;
    setBusy(true);
    setError(null);
    setReport(null);
    try {
      const snapshot = await executionAnalyticsService.captureBalanceSnapshot();
      if (!alive.current || sequence !== request.current) return;
      setSnapshots((rows) => [snapshot, ...rows].slice(0, 20));
      setOpening((value) => value || snapshot.id);
      setClosing(snapshot.id);
    } catch (cause) {
      if (alive.current && sequence === request.current) setError(getApiErrorMessage(cause));
    } finally {
      if (alive.current && sequence === request.current) setBusy(false);
    }
  }
  async function compare() {
    const sequence = ++request.current;
    setBusy(true);
    setError(null);
    setReport(null);
    try {
      const result = await executionAnalyticsService.getLiveReconciliation(opening, closing);
      if (alive.current && sequence === request.current) setReport(result);
    } catch (cause) {
      if (alive.current && sequence === request.current) setError(getApiErrorMessage(cause));
    } finally {
      if (alive.current && sequence === request.current) setBusy(false);
    }
  }
  async function save() {
    const sequence = ++request.current;
    setBusy(true);
    setError(null);
    try {
      const result = await executionAnalyticsService.freezeLiveReconciliation(opening, closing);
      if (alive.current && sequence === request.current) setReport(result);
    } catch (cause) {
      if (alive.current && sequence === request.current) setError(getApiErrorMessage(cause));
    } finally {
      if (alive.current && sequence === request.current) setBusy(false);
    }
  }
  function download() {
    if (!report) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }),
    );
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `reconciliation-${report.input_hash.slice(0, 12)}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  }
  if (!available) return null;
  return (
    <section aria-label="Live account reconciliation" className="surface-panel p-5 space-y-3">
      <h3 className="font-semibold text-white">Live account reconciliation</h3>
      <p className="text-soft text-sm">
        Compare recorded fills with read-only balance observations. Missing costs and unexplained
        cash or token changes remain unknown. Wallet holdings come from venue-indexed discovery and
        native balance reads.
      </p>
      <button className="btn-secondary" disabled={busy} onClick={() => void capture()}>
        Capture live balances
      </button>
      <div className="flex flex-wrap gap-3">
        <label>
          Opening observation
          <select
            aria-label="Opening observation"
            className="block bg-surface border border-border"
            disabled={busy}
            value={opening}
            onChange={(event) => {
              setOpening(event.currentTarget.value);
              setReport(null);
            }}
          >
            <option value="">Choose observation</option>
            {snapshots.map((row) => (
              <option key={row.id} value={row.id}>
                {new Date(row.observed_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
        <label>
          Closing observation
          <select
            aria-label="Closing observation"
            className="block bg-surface border border-border"
            disabled={busy}
            value={closing}
            onChange={(event) => {
              setClosing(event.currentTarget.value);
              setReport(null);
            }}
          >
            <option value="">Choose observation</option>
            {snapshots.map((row) => (
              <option key={row.id} value={row.id}>
                {new Date(row.observed_at).toLocaleString()}
              </option>
            ))}
          </select>
        </label>
        <button
          className="btn-secondary"
          disabled={busy || !opening || !closing || opening === closing}
          onClick={() => void compare()}
        >
          Compare observations
        </button>
      </div>
      {busy && <p role="status">Reading account evidence…</p>}
      {error && (
        <p role="alert" className="text-red-400">
          {error}
        </p>
      )}
      {report && (
        <div className="space-y-2" aria-live="polite">
          <p>Reconciliation: {report.status === "reconciled" ? "Reconciled" : "Incomplete"}</p>
          <p>Gross realized P&amp;L: {money(report.gross_realized_pnl)}</p>
          <p>Net realized P&amp;L: {money(report.net_realized_pnl)}</p>
          <p>Verified net realized P&amp;L: {money(report.verified_net_realized_pnl)}</p>
          <p>Indicative unrealized P&amp;L: {money(report.unrealized_pnl)}</p>
          <p>Indicative mark contribution: {money(report.mark_contribution)}</p>
          <p>Unexplained cash change: {money(report.cash_residual_usdc)}</p>
          <p>Confirmed fees: {money(report.total_fees_usdc)}</p>
          <p>
            Confirmed rebates: {money(report.confirmed_rebates_usdc)}; estimated rebates:{" "}
            {money(report.estimated_rebates_usdc)}
          </p>
          <ul>
            {report.reasons.map((reason) => (
              <li key={reason}>{reason.replace(/_/g, " ").toLowerCase()}</li>
            ))}
          </ul>
          <p>
            {report.source_ids.length} confirmed fill references; {report.history_source_ids.length}{" "}
            opening-history references
          </p>
          <details>
            <summary>
              Acceptance evidence (
              {report.acceptance.filter((check) => check.status === "pass").length}/
              {report.acceptance.length} passed)
            </summary>
            <ul>
              {report.acceptance.map((check) => (
                <li key={check.criterion}>
                  {check.criterion.replace(/_/g, " ")}: {check.status} —{" "}
                  {check.reason.replace(/_/g, " ").toLowerCase()}
                </li>
              ))}
            </ul>
            <p>
              Release decision:{" "}
              {report.confidence.release_decision.replace(/_/g, " ").toLowerCase()}
            </p>
          </details>
          <details>
            <summary>Position residuals</summary>
            {Object.entries(report.position_residuals).map(([token, quantity]) => (
              <p key={token} className="break-all">
                Token {token}: {quantity} shares
              </p>
            ))}
          </details>
          <details>
            <summary>Point-in-time benchmarks</summary>
            <p>
              Policy: {report.benchmark_version}. Price differences include latency and market
              movement; these are not causal impact or realized returns.
            </p>
            {report.benchmarks.slice(0, 10).map((row) => (
              <div key={row.benchmark_id} className="border-t border-border py-2">
                <p>
                  Fill reference {row.source_id}:{" "}
                  {row.status === "abstain" ? "Unavailable" : "Observed comparison"}
                </p>
                <p>
                  Implementation shortfall: {money(row.implementation_shortfall)}; spread:{" "}
                  {money(row.spread_cost)}; remaining execution difference:{" "}
                  {money(row.execution_difference)}
                </p>
                {row.reason && <p>{row.reason.replace(/_/g, " ").toLowerCase()}</p>}
                {row.capture_id && <p className="break-all">Book: {row.capture_id}</p>}
              </div>
            ))}
            <p>Download evidence for all benchmarks and cost allocations.</p>
          </details>
          <details>
            <summary>External funding and activity</summary>
            <p>Source quality: {report.external_flows.quality.replace(/_/g, " ")}</p>
            <p>
              Funding cashflow: {money(report.external_flows.funding_cashflow)}; unattributed
              rebates: {money(report.external_flows.unattributed_rebates)}
            </p>
            {report.external_flows.reasons.map((reason) => (
              <p key={reason}>{reason.replace(/_/g, " ").toLowerCase()}</p>
            ))}
          </details>
          {report.report_id ? (
            <p className="break-all">Saved report: {report.report_id}</p>
          ) : (
            <button className="btn-secondary" disabled={busy} onClick={() => void save()}>
              Save immutable report
            </button>
          )}
          <button className="btn-secondary" onClick={download}>
            Download reconciliation evidence
          </button>
        </div>
      )}
    </section>
  );
}
