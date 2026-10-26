import { useState } from "react";
import { apiClient } from "../services/apiClient";
import { getApiErrorMessage } from "../utils/apiError";

interface Cost {
  status: string;
  reason: string | null;
  matched_shares: string;
  vwap: string | null;
  net_cash: string | null;
  coverage: string;
  partial: boolean;
  requested: { amount: string };
  confidence: string;
}
interface Result {
  condition_id: string;
  status: string;
  reason: string | null;
  as_of: string;
  capture_ids: string[];
  metadata_id?: string;
  rule_hash?: string;
  books: Record<
    string,
    { grade: string; reasons: string[]; book_hash: string | null; age_seconds: string | null }
  >;
  estimates: Cost[];
}

const displayDecimal = (value: string | null) => {
  if (value === null) return "Unavailable";
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString("en-US", { maximumFractionDigits: 6, useGrouping: false })
    : value;
};

export default function BookResearchPanel() {
  const [condition, setCondition] = useState("");
  const [outcome, setOutcome] = useState("YES");
  const [side, setSide] = useState("BUY");
  const [sizes, setSizes] = useState("5,20,40");
  const [fees, setFees] = useState("");
  const [cutoff, setCutoff] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [recipe, setRecipe] = useState<Record<string, unknown> | null>(null);
  const [runId, setRunId] = useState("");
  const [executionId, setExecutionId] = useState("");
  const [comparison, setComparison] = useState<{
    reason?: string;
    observed_vwap_difference?: string | null;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  async function inspect() {
    setBusy(true);
    setError("");
    setNotice("");
    setResult(null);
    setRecipe(null);
    const query = {
      condition_id: condition,
      outcome,
      side,
      sizes: sizes.split(",").map((s) => s.trim()),
      fee_bps: fees || null,
      fee_version: fees ? "user-flat-notional-bps-v1" : null,
      as_of: cutoff || null,
      lot: "0.01",
    };
    try {
      const data = await apiClient.post<Result>("/api/microstructure/analyze", query);
      setResult(data);
      setRecipe({ ...query, as_of: data.as_of, capture_ids: data.capture_ids });
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  async function capture() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const data = await apiClient.post<{ status: string }>("/api/microstructure/capture", {
        condition_id: condition,
      });
      setNotice(
        `Current public capture: ${data.status}. Inspect again to use newly received evidence.`,
      );
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  async function freeze() {
    if (!recipe) return;
    setBusy(true);
    setError("");
    try {
      const data = await apiClient.post<{ id: string; result: Result }>(
        "/api/microstructure/runs",
        recipe,
      );
      setRunId(data.id);
      setNotice(`Frozen research: ${data.id}`);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  async function replay() {
    setBusy(true);
    setError("");
    setResult(null);
    setRecipe(null);
    try {
      const data = await apiClient.get<{ result: Result }>(
        `/api/microstructure/runs/${encodeURIComponent(runId)}`,
      );
      setResult(data.result);
      setNotice("Verified frozen replay; later observations excluded.");
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  async function compareExecution() {
    setBusy(true);
    setError("");
    setComparison(null);
    try {
      setComparison(
        await apiClient.get(
          `/api/microstructure/runs/${encodeURIComponent(runId)}/executions/${encodeURIComponent(executionId)}`,
        ),
      );
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section
      aria-label="Binary book research"
      className="rounded-xl border border-white/10 bg-surface-secondary p-4 space-y-3"
    >
      <h2 className="text-lg font-semibold text-white">Binary book research</h2>
      <p className="text-soft text-sm">
        Read-only displayed liquidity and counterfactual costs. These are not fills, measured market
        impact, or maker fill probabilities. YES and NO liquidity is never freely netted.
      </p>
      <fieldset disabled={busy} className="flex flex-wrap gap-3">
        <legend className="sr-only">Book research inputs</legend>
        <label>
          Condition ID
          <input
            className="input-field block"
            value={condition}
            onChange={(e) => setCondition(e.target.value)}
          />
        </label>
        <label>
          Outcome
          <select
            className="input-field block"
            value={outcome}
            onChange={(e) => setOutcome(e.target.value)}
          >
            <option>YES</option>
            <option>NO</option>
          </select>
        </label>
        <label>
          Book side
          <select
            className="input-field block"
            value={side}
            onChange={(e) => setSide(e.target.value)}
          >
            <option>BUY</option>
            <option>SELL</option>
          </select>
        </label>
        <label>
          Share sizes (comma-separated, up to 8)
          <input
            className="input-field block"
            value={sizes}
            onChange={(e) => setSizes(e.target.value)}
          />
        </label>
        <label>
          Flat fee assumption (basis points)
          <input
            className="input-field block"
            type="number"
            min="0"
            max="10000"
            value={fees}
            onChange={(e) => setFees(e.target.value)}
            placeholder="Unknown until explicitly entered"
          />
        </label>
        <label>
          Book cutoff (ISO with timezone)
          <input
            className="input-field block"
            value={cutoff}
            onChange={(e) => setCutoff(e.target.value)}
            placeholder="Current server time"
          />
        </label>
        <button
          className="btn-secondary"
          onClick={capture}
          disabled={!/^0x[a-fA-F0-9]{64}$/.test(condition)}
        >
          Capture current public books
        </button>
        <button
          className="btn-primary"
          onClick={inspect}
          disabled={!/^0x[a-fA-F0-9]{64}$/.test(condition)}
        >
          Inspect binary book
        </button>
      </fieldset>
      <p className="text-soft text-sm">
        Share rounding uses an explicit 0.01-share model assumption; native minimum order size and
        tick rules still apply. Fees above are a flat model assumption, not a verified venue fee
        tier.
      </p>
      {error && <p role="alert">{error}</p>}
      {notice && (
        <p role="status" className="break-all">
          {notice}
        </p>
      )}
      {result && (
        <div className="space-y-2">
          <p>
            Captured condition: {result.condition_id} · As of: {result.as_of} ·{" "}
            {result.reason ?? "Counterfactual displayed-depth estimate"}
          </p>
          {Object.entries(result.books).map(([label, book]) => (
            <p key={label} role="status">
              {label}: {book.grade} · Age {book.age_seconds ?? "unknown"} seconds ·{" "}
              {book.reasons.join(", ")}
            </p>
          ))}
          {!!result.estimates.length && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <caption>
                  Displayed depth cost estimates · display rounded to 6 decimals; exact values
                  retained in recipes
                </caption>
                <thead>
                  <tr>
                    <th className="px-2 py-2">Requested shares</th>
                    <th className="px-2 py-2">Matched shares</th>
                    <th className="px-2 py-2">VWAP</th>
                    <th className="px-2 py-2">Modeled cash incl. fees</th>
                    <th className="px-2 py-2">Coverage</th>
                    <th className="px-2 py-2">Evidence</th>
                  </tr>
                </thead>
                <tbody>
                  {result.estimates.map((cost, i) => (
                    <tr key={i}>
                      <td className="px-2 py-2">{cost.requested.amount}</td>
                      <td className="px-2 py-2" title={cost.matched_shares}>
                        {displayDecimal(cost.matched_shares)}
                      </td>
                      <td className="px-2 py-2" title={cost.vwap ?? undefined}>
                        {displayDecimal(cost.vwap)}
                      </td>
                      <td className="px-2 py-2" title={cost.net_cash ?? undefined}>
                        {displayDecimal(cost.net_cash)}
                      </td>
                      <td className="px-2 py-2" title={cost.coverage}>
                        {displayDecimal(cost.coverage)}
                      </td>
                      <td className="px-2 py-2">
                        {cost.reason ??
                          (cost.partial ? "Partial displayed depth" : cost.confidence)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="text-soft break-all">
            Rule hash: {result.rule_hash ?? "Unavailable"} · Metadata:{" "}
            {result.metadata_id ?? "Unavailable"}
          </p>
          {recipe && (
            <button className="btn-secondary" disabled={busy} onClick={freeze}>
              Freeze book research
            </button>
          )}
        </div>
      )}
      <div className="flex gap-3">
        <label>
          Frozen research ID
          <input
            className="input-field block"
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            disabled={busy}
          />
        </label>
        <button className="btn-secondary" disabled={busy || !runId} onClick={replay}>
          Replay frozen book research
        </button>
      </div>
      <div className="flex gap-3">
        <label>
          Recorded execution trade ID
          <input
            className="input-field block"
            value={executionId}
            onChange={(e) => setExecutionId(e.target.value)}
            disabled={busy}
          />
        </label>
        <button
          className="btn-secondary"
          disabled={busy || !runId || !/^[0-9]+$/.test(executionId)}
          onClick={compareExecution}
        >
          Compare recorded execution
        </button>
      </div>
      {comparison && (
        <p role="status">
          Recorded execution comparison:{" "}
          {comparison.observed_vwap_difference ?? comparison.reason ?? "Unavailable"}. This price
          difference includes latency and market movement; it is not causal price impact.
        </p>
      )}
    </section>
  );
}
