import { useCallback, useEffect, useRef, useState } from "react";
import { latencyArbService, LatencyPaperHistory as History } from "../services/latencyArbService";

const labels: Record<string, string> = {
  ESTIMATED: "Estimated",
  PARTIALLY_FILLED_MODELED: "Partial estimate",
  UNFILLED: "Unfilled",
  UNVERIFIABLE_DATA: "Unverifiable",
};

export default function LatencyPaperHistory() {
  const [history, setHistory] = useState<History | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState(false);
  const generation = useRef(0);
  const invalidate = useCallback(() => {
    generation.current++;
  }, []);
  const load = useCallback(async () => {
    const request = ++generation.current;
    try {
      const result = await latencyArbService.getPaperIntents({ limit: 20, offset });
      if (request === generation.current) {
        setHistory(result);
        setError(false);
      }
    } catch {
      if (request === generation.current) setError(true);
    }
  }, [offset]);
  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), 5000);
    const changed = () => {
      invalidate();
      setHistory(null);
      setOffset(0);
      if (offset === 0) void load();
    };
    window.addEventListener("polymarket:active-account-changed", changed);
    return () => {
      invalidate();
      window.clearInterval(timer);
      window.removeEventListener("polymarket:active-account-changed", changed);
    };
  }, [load, offset, invalidate]);
  return (
    <section className="surface-panel p-5 space-y-3" aria-label="Paper execution evidence">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <h2 className="text-lg font-semibold">Paper execution evidence</h2>
        <button className="btn-muted" onClick={() => void load()}>
          Refresh evidence
        </button>
      </div>
      <p className="text-sm text-soft">
        Estimates use recorded native depth and explicit assumed fees. They are never
        exchange-confirmed. Model edge, estimated execution costs and realized paper P&amp;L are
        separate from live settled P&amp;L.
      </p>
      {error && <p role="alert">Paper history could not be loaded.</p>}
      {history?.intents.some(({ result }) => result.execution_state === "UNVERIFIABLE_DATA") && (
        <p role="status">
          Missing or invalid execution evidence: not fill-verifiable. No inferred realized returns.
        </p>
      )}
      {!history?.intents.length && !error && <p>No paper decisions recorded for this account.</p>}
      {!!history?.intents.length && (
        <div className="overflow-x-auto">
          <table className="table-theme text-sm w-full">
            <thead>
              <tr>
                <th>Outcome</th>
                <th>Reason</th>
                <th>Model probability</th>
                <th>Measured delay</th>
                <th>Estimated shares</th>
                <th>Cost incl. fees</th>
                <th>Realized paper P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {history.intents.map(({ intent_id, intent, result }) => (
                <tr key={intent_id}>
                  <td>{labels[result.execution_state] ?? "Unverifiable"}</td>
                  <td>{result.reason}</td>
                  <td>{(Number(intent.window.model_probability) * 100).toFixed(1)}%</td>
                  <td>{intent.window.measured_delay_ms} ms</td>
                  <td>{result.shares}</td>
                  <td>${(Number(result.notional_usdc) + Number(result.fee_usdc)).toFixed(4)}</td>
                  <td>
                    {result.execution_state === "UNVERIFIABLE_DATA"
                      ? "—"
                      : `$${Number(result.realized_pnl).toFixed(4)}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="flex gap-3">
        <button
          disabled={!offset}
          onClick={() => {
            setHistory(null);
            setOffset(Math.max(0, offset - 20));
          }}
        >
          Previous evidence
        </button>
        <button
          disabled={!history || offset + 20 >= history.total}
          onClick={() => {
            setHistory(null);
            setOffset(offset + 20);
          }}
        >
          Next evidence
        </button>
      </div>
    </section>
  );
}
