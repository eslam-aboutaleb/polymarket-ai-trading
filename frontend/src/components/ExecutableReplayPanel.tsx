import { useState } from "react";
import { apiClient } from "../services/apiClient";
import { getApiErrorMessage } from "../utils/apiError";

interface Scenario {
  net_pnl: string | null;
  gross_pnl_on_matched_quantity: string | null;
  fees_usdc: string;
  trade_count: number;
  max_drawdown: string | null;
  splits: { test: { total_return: string | null; return_samples: number } };
  statistics: { sharpe_ratio: number | null; reason?: string; return_sample_count?: number };
  [key: string]: unknown;
}
interface FrozenRun {
  id: string;
  input_hash: string;
  evidence: { book_evidence: string; signal_evidence: string };
  definition: { trials: { id: string; strategy_version: string }[] };
  result: {
    execution_evidence: string;
    selected_trial_id: string | null;
    holdout: Scenario | null;
    [key: string]: unknown;
  } | null;
}

const show = (value: unknown) => (value == null ? "Unavailable" : String(value));

export default function ExecutableReplayPanel() {
  const [recipe, setRecipe] = useState("");
  const [identity, setIdentity] = useState("");
  const [run, setRun] = useState<FrozenRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const base = "/api/backtesting/executable/runs";

  async function action(kind: "freeze" | "execute" | "load" | "verify") {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (kind === "verify") {
        const data = await apiClient.post<{ identical: boolean }>(`${base}/${run!.id}/verify`);
        setNotice(
          data.identical ? "Deterministic replay verified: identical result." : "Replay differs.",
        );
      } else {
        const data =
          kind === "freeze"
            ? await apiClient.post<FrozenRun>(base, JSON.parse(recipe))
            : kind === "execute"
              ? await apiClient.post<FrozenRun>(`${base}/${run!.id}/execute`)
              : await apiClient.get<FrozenRun>(`${base}/${encodeURIComponent(identity.trim())}`);
        setRun(data);
        setIdentity(data.id);
      }
    } catch (err) {
      setError(getApiErrorMessage(err, "Executable replay unavailable"));
    } finally {
      setBusy(false);
    }
  }

  const selected = run?.result?.holdout;
  return (
    <section
      aria-label="Executable replay"
      className="bg-gray-900 border border-gray-700 rounded-lg p-5 space-y-4"
    >
      <h3 className="text-lg font-semibold">Frozen executable replay</h3>
      <p className="text-sm text-gray-400">
        Research simulations using captured depth, explicit costs and periodic marked NAV. Freeze
        every attempted strategy and chronological split before evaluating the holdout. Imported
        data remains unverified. Modeled fills do not confirm exchange execution.
      </p>
      <label className="block text-sm">
        Replay recipe JSON
        <textarea
          aria-label="Replay recipe JSON"
          value={recipe}
          onChange={(e) => setRecipe(e.target.value)}
          className="block w-full h-40 mt-2 bg-gray-950 border border-gray-600 rounded p-3 font-mono text-xs"
          placeholder={
            '{"definition": {"start_at": "…", "trials": […], "tape": {…}}, "capture_ids": […]}'
          }
        />
      </label>
      <p className="text-xs text-gray-400">
        Supply a versioned definition with funding, sampling interval, train/validation/test
        boundaries, strategy family, fees, latency and tape. Optional capture_ids bind actual
        archive receipts; follower_wallet uses native first-receipt signals. View the API schema for
        the full contract.
      </p>
      <div className="flex flex-wrap gap-3">
        <button
          disabled={busy || !recipe.trim()}
          onClick={() => action("freeze")}
          className="px-3 py-2 bg-blue-700 rounded disabled:opacity-40"
        >
          Freeze recipe
        </button>
        <button
          disabled={busy || !run || !!run.result}
          onClick={() => action("execute")}
          className="px-3 py-2 bg-blue-700 rounded disabled:opacity-40"
        >
          Evaluate frozen recipe
        </button>
        <button
          disabled={busy || !run?.result}
          onClick={() => action("verify")}
          className="px-3 py-2 border rounded disabled:opacity-40"
        >
          Verify replay
        </button>
      </div>
      <div className="flex gap-3">
        <input
          aria-label="Frozen replay ID"
          value={identity}
          onChange={(e) => setIdentity(e.target.value)}
          placeholder="Frozen replay ID"
          className="bg-gray-950 border rounded p-2 flex-1"
        />
        <button disabled={busy || !identity.trim()} onClick={() => action("load")}>
          Load replay
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
      {run && (
        <div className="space-y-3">
          <p className="text-xs break-all">Frozen input: {run.input_hash}</p>
          <p>
            Book evidence: {run.evidence.book_evidence}. Signal evidence:{" "}
            {run.evidence.signal_evidence}.
          </p>
          <p>
            Frozen trials:{" "}
            {run.definition.trials.map((t) => `${t.id} (${t.strategy_version})`).join(", ")}
          </p>
          {run.result && (
            <>
              <p>
                {run.result.execution_evidence}. Selected trial:{" "}
                {show(run.result.selected_trial_id)}.
              </p>
              {selected ? (
                <dl className="grid grid-cols-2 md:grid-cols-4 gap-4">
                  {Object.entries({
                    "Full replay net P&L (USDC)": selected.net_pnl,
                    "Gross P&L (USDC)": selected.gross_pnl_on_matched_quantity,
                    "Fees (USDC)": selected.fees_usdc,
                    "Modeled fills": selected.trade_count,
                    "Maximum drawdown": selected.max_drawdown,
                    "Periodic NAV Sharpe": selected.statistics.sharpe_ratio,
                    "Test window return": selected.splits.test.total_return,
                    "Test return samples": selected.splits.test.return_samples,
                  }).map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-xs text-gray-400">{label}</dt>
                      <dd>{show(value)}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <p>Holdout unavailable: validation did not yield a complete marked NAV.</p>
              )}
              <details>
                <summary>Full report, assumptions and ledger</summary>
                <pre className="text-xs overflow-auto max-h-96 p-3">
                  {JSON.stringify(run.result, null, 2)}
                </pre>
              </details>
            </>
          )}
        </div>
      )}
    </section>
  );
}
