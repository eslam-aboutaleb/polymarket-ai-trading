import { useEffect, useRef, useState } from "react";
import { marketContextService, type MarketContext } from "../../services/marketContextService";
import { getApiErrorMessage } from "../../utils/apiError";

export default function ObservationHistory({ conditionId }: { conditionId: string }) {
  const [rows, setRows] = useState<MarketContext[] | null>(null);
  const [asOf, setAsOf] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef(0);
  useEffect(
    () => () => {
      request.current++;
    },
    [],
  );

  const load = async () => {
    const current = ++request.current;
    setLoading(true);
    setError(null);
    setRows(null);
    try {
      const observations = await marketContextService.history(
        conditionId,
        asOf ? `${asOf}Z` : undefined,
      );
      if (current === request.current) setRows(observations);
    } catch (err: unknown) {
      if (current === request.current)
        setError(getApiErrorMessage(err, "Observation history unavailable"));
    } finally {
      if (current === request.current) setLoading(false);
    }
  };

  return (
    <section className="surface-panel p-6" aria-labelledby="history-heading">
      <h2 id="history-heading" className="text-lg font-bold">
        Source observation history
      </h2>
      <p className="my-3 text-sm">
        Review up to 25 observations captured by this deployment. These are historical source
        records, not executable quotes or historical order-book depth.
      </p>
      <label className="block text-sm" htmlFor="history-as-of">
        Received by (UTC, optional)
      </label>
      <input
        id="history-as-of"
        type="datetime-local"
        className="input-field mt-2 mb-3 max-w-full"
        value={asOf}
        onChange={(event) => setAsOf(event.target.value)}
      />
      <button className="btn-muted block" onClick={() => void load()} disabled={loading}>
        Load observation history
      </button>
      {loading && <p role="status">Loading observation history…</p>}
      {error && (
        <p role="alert" className="mt-3">
          {error}
        </p>
      )}
      {rows?.length === 0 && <p className="mt-3">No observations were captured by this time.</p>}
      {rows && (
        <ol aria-label="Captured observations" className="mt-4 space-y-3">
          {rows.map((row) => (
            <li key={row.snapshot_id}>
              <details>
                <summary className="cursor-pointer">
                  Received {new Date(row.observed_at).toISOString()} ·{" "}
                  {row.outcomes
                    .map(
                      (o) =>
                        `${o.outcome} ${o.indicative_price === null ? "unavailable" : `${Math.round(o.indicative_price * 100)}¢`}`,
                    )
                    .join(" / ")}
                </summary>
                <p className="my-2 whitespace-pre-wrap">{row.rules.text || "Rules unavailable"}</p>
                <p className="text-xs break-all">Rule identity: {row.rules.content_hash}</p>
                <p className="text-xs break-all">Snapshot: {row.snapshot_id}</p>
              </details>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
