import { useState } from "react";
import { apiClient } from "../services/apiClient";
import { getApiErrorMessage } from "../utils/apiError";

interface Pnl {
  funding: string;
  gross_pnl: string | null;
  net_pnl: string | null;
  fees: string | null;
  fee_status: string;
  nav: string | null;
  realized_pnl: string;
}
interface Fill {
  fill_id: string;
  order_id: string;
  side: string;
  price: string;
  shares: string;
  fee: string | null;
  fee_status: string;
  liquidity: string;
  depth_quality: string;
}
interface Report {
  mode: string;
  result_class: string;
  result_hash: string;
  run: {
    quality_status: string;
    data_snapshot_id: string;
    parameters_hash: string;
    fee_schedule_id: string | null;
    seed: number;
    benchmark_id: string | null;
  };
  pnl: Pnl;
  downside: { max_drawdown_usdc: string | null; equity_samples: number };
  conservation: { holds: boolean };
  uncertainty: string[];
  fills: Fill[];
}
interface ComparisonRow {
  name: string;
  net_pnl: string | null;
  gross_pnl: string | null;
  fees: string | null;
  fee_status: string;
  max_drawdown_usdc: string | null;
  fills: number;
  quality_status: string;
}
interface LabRun {
  id: number;
  run_type: "event_replay" | "shadow_run";
  mode_watermark: string;
  net_pnl_quality: string;
  report: Report;
  baselines: Record<string, Report> | null;
  benchmark: {
    same_data_snapshot: boolean;
    net_pnl_minus_baseline: Record<string, string | null>;
    comparison: { rows: ComparisonRow[]; ranking: string[]; unranked_unknown_net_pnl: string[] };
  } | null;
}

/** Missing cost data is "Unknown", never rendered as zero. */
const money = (value: string | null | undefined) =>
  value == null ? "Unknown" : `${Number(value).toFixed(4)} USDC`;

const BASE = "/api/backtesting/lab";

export default function EventReplayLabPanel() {
  const [recipe, setRecipe] = useState("");
  const [run, setRun] = useState<LabRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function submit(kind: "event-replays" | "shadow-runs") {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      setRun(await apiClient.post<LabRun>(`${BASE}/${kind}`, JSON.parse(recipe)));
    } catch (err) {
      setError(getApiErrorMessage(err, "Strategy laboratory unavailable"));
    } finally {
      setBusy(false);
    }
  }

  async function verify() {
    if (!run) return;
    setBusy(true);
    setError("");
    try {
      const data = await apiClient.post<{ identical: boolean }>(
        `${BASE}/event-replays/${run.id}/verify`,
      );
      setNotice(
        data.identical
          ? "Deterministic replay verified: identical result."
          : "Replay differs from the stored result.",
      );
    } catch (err) {
      setError(getApiErrorMessage(err, "Verification failed"));
    } finally {
      setBusy(false);
    }
  }

  const report = run?.report;
  return (
    <section
      aria-label="Event replay laboratory"
      className="bg-gray-900 border border-gray-700 rounded-lg p-5 space-y-4"
    >
      <h3 className="text-lg font-semibold">Event replay and shadow laboratory</h3>
      <p className="text-sm text-gray-400">
        Deterministic event-time replay with decision and submission latency, displayed depth,
        partial fills and explicit costs. Results are simulated research output, not exchange
        confirmed. Shadow runs simulate decisions on observed books and never send or sign an order.
      </p>
      <label className="block text-sm">
        Replay recipe JSON
        <textarea
          aria-label="Event replay recipe JSON"
          value={recipe}
          onChange={(e) => setRecipe(e.target.value)}
          className="block w-full h-40 mt-2 bg-gray-950 border border-gray-600 rounded p-3 font-mono text-xs"
          placeholder={'{"events": […], "config": {…}, "strategy": {"name": "follow_signals", …}}'}
        />
      </label>
      <div className="flex flex-wrap gap-3">
        <button
          disabled={busy || !recipe.trim()}
          onClick={() => submit("event-replays")}
          className="px-3 py-2 bg-blue-700 rounded disabled:opacity-40"
        >
          Run replay
        </button>
        <button
          disabled={busy || !recipe.trim()}
          onClick={() => submit("shadow-runs")}
          className="px-3 py-2 bg-blue-700 rounded disabled:opacity-40"
        >
          Run shadow
        </button>
        <button
          disabled={busy || !run || run.run_type !== "event_replay"}
          onClick={verify}
          className="px-3 py-2 border rounded disabled:opacity-40"
        >
          Verify lab replay
        </button>
      </div>
      {error && (
        <p role="alert" className="text-red-400">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="text-green-400">
          {notice}
        </p>
      )}
      {run && report && (
        <div className="space-y-3">
          <p role="note" className="border border-yellow-600 text-yellow-300 rounded p-2 text-sm">
            {run.mode_watermark}
          </p>
          <p className="text-xs break-all">
            Snapshot {report.run.data_snapshot_id} · seed {report.run.seed} · quality{" "}
            {report.run.quality_status} · result {report.result_hash}
          </p>
          <dl className="grid grid-cols-2 md:grid-cols-4 gap-4">
            {Object.entries({
              "Net P&L": money(report.pnl.net_pnl),
              "Gross P&L": money(report.pnl.gross_pnl),
              Fees: `${money(report.pnl.fees)} (${report.pnl.fee_status})`,
              "Max drawdown": money(report.downside.max_drawdown_usdc),
              "Modeled fills": report.fills.length,
              "Ledger conserves value": report.conservation.holds ? "Yes" : "No",
            }).map(([label, value]) => (
              <div key={label}>
                <dt className="text-xs text-gray-400">{label}</dt>
                <dd>{String(value)}</dd>
              </div>
            ))}
          </dl>
          {report.uncertainty.length > 0 && (
            <ul aria-label="Uncertainty" className="list-disc pl-5 text-sm text-yellow-300">
              {report.uncertainty.map((u) => (
                <li key={u}>{u}</li>
              ))}
            </ul>
          )}
          {run.benchmark && (
            <table aria-label="Benchmark comparison" className="text-sm w-full">
              <thead>
                <tr>
                  <th className="text-left">Run</th>
                  <th className="text-left">Net P&L</th>
                  <th className="text-left">Fees</th>
                  <th className="text-left">Drawdown</th>
                  <th className="text-left">Fills</th>
                  <th className="text-left">Quality</th>
                </tr>
              </thead>
              <tbody>
                {run.benchmark.comparison.rows.map((row) => (
                  <tr key={row.name}>
                    <td>{row.name}</td>
                    <td>{money(row.net_pnl)}</td>
                    <td>{money(row.fees)}</td>
                    <td>{money(row.max_drawdown_usdc)}</td>
                    <td>{row.fills}</td>
                    <td>{row.quality_status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <details>
            <summary>Fills and full report</summary>
            <pre className="text-xs overflow-auto max-h-96 p-3">
              {JSON.stringify(report, null, 2)}
            </pre>
          </details>
        </div>
      )}
    </section>
  );
}
