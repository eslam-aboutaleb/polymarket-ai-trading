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
        cash or token changes remain unknown. Observations cover application-traded tokens.
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
          <p>Net realized P&amp;L: {money(report.net_realized_pnl)}</p>
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
          <p>{report.source_ids.length} confirmed fill references</p>
          <button className="btn-secondary" onClick={download}>
            Download reconciliation evidence
          </button>
        </div>
      )}
    </section>
  );
}
