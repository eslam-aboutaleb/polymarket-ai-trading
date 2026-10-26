import { FormEvent, useState } from "react";
import { apiClient } from "../services/apiClient";
import { getApiErrorMessage } from "../utils/apiError";

interface Observation {
  snapshot_id: string;
  event_at: string | null;
  received_at: string;
  sequence: string | null;
  quality: string[];
  normalized: { kind: string; status?: string; bids?: { price: string; shares: string }[] };
}
interface Quality {
  source_status: string;
  stale: boolean;
  age_seconds: number | null;
  flags: string[];
}

export default function MarketDataQuality() {
  const [token, setToken] = useState("");
  const [cutoff, setCutoff] = useState("");
  const [loaded, setLoaded] = useState<{ token: string; cutoff: string } | null>(null);
  const [rows, setRows] = useState<Observation[]>([]);
  const [quality, setQuality] = useState<Quality | null>(null);
  const [dataset, setDataset] = useState("");
  const [replayId, setReplayId] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function inspect(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setRows([]);
    setQuality(null);
    setDataset("");
    setLoaded(null);
    const asOf = cutoff || new Date().toISOString();
    const params = new URLSearchParams({ token_id: token, as_of: asOf, limit: "50" });
    try {
      const [result, health] = await Promise.all([
        apiClient.get<{ observations: Observation[] }>(`/api/market-data/observations?${params}`),
        apiClient.get<Quality>(`/api/market-data/quality?token_id=${encodeURIComponent(token)}`),
      ]);
      setRows(result.observations);
      setQuality(health);
      setLoaded({ token, cutoff: asOf });
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function freeze() {
    if (!loaded) return;
    setBusy(true);
    setError("");
    try {
      const params = new URLSearchParams({
        token_id: loaded.token,
        as_of: loaded.cutoff,
        limit: "50",
      });
      const result = await apiClient.post<{ dataset_id: string }>(
        `/api/market-data/datasets?${params}`,
      );
      setDataset(result.dataset_id);
      setReplayId(result.dataset_id);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  async function replay() {
    setBusy(true);
    setError("");
    setRows([]);
    setQuality(null);
    setLoaded(null);
    setDataset("");
    try {
      const result = await apiClient.get<{ observations: Observation[]; dataset_id: string }>(
        `/api/market-data/datasets/${encodeURIComponent(replayId)}`,
      );
      setRows(result.observations);
      setDataset(result.dataset_id);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Market data quality" className="card space-y-3 p-4">
      <h2 className="text-lg font-semibold text-white">Market data quality</h2>
      <p className="text-soft text-sm">
        Inspect captured observations before evaluating a strategy. Missing or interrupted depth is
        insufficient for executable fill claims.
      </p>
      <form onSubmit={inspect} className="flex flex-wrap gap-3 items-end">
        <label>
          Token ID
          <input
            className="input block"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            required
            pattern="[0-9]{1,120}"
            disabled={busy}
          />
        </label>
        <label>
          As of (ISO timestamp with timezone)
          <input
            className="input block"
            value={cutoff}
            onChange={(e) => setCutoff(e.target.value)}
            placeholder="Latest received observations"
            disabled={busy}
          />
        </label>
        <button className="btn-secondary" disabled={busy}>
          Inspect observations
        </button>
      </form>
      {error && <p role="alert">{error}</p>}
      {quality && (
        <div role="status">
          <p>
            Source: {quality.source_status} ·{" "}
            {quality.stale ? "Stale or unavailable" : "Fresh receipt"}
          </p>
          <p>
            Receipt age:{" "}
            {quality.age_seconds === null
              ? "Unknown"
              : `${Math.round(quality.age_seconds)} seconds`}
          </p>
          <p>Quality: INSUFFICIENT_DATA · {quality.flags.join(", ")}</p>
        </div>
      )}
      {loaded && (
        <p>
          Captured token: {loaded.token} · Cutoff: {loaded.cutoff} · {rows.length} captured
          observations (up to 50)
        </p>
      )}
      {loaded && (
        <button className="btn-secondary" disabled={busy || !rows.length} onClick={freeze}>
          Freeze these observations
        </button>
      )}
      <div className="flex flex-wrap gap-3">
        <label>
          Dataset ID
          <input
            className="input block"
            value={replayId}
            onChange={(e) => setReplayId(e.target.value)}
            disabled={busy}
          />
        </label>
        <button
          className="btn-secondary"
          disabled={busy || !/^[a-f0-9]{64}$/.test(replayId)}
          onClick={replay}
        >
          Replay frozen observations
        </button>
      </div>
      {dataset && (
        <p role="status" className="break-all">
          Frozen dataset: {dataset} · {rows.length} observations
        </p>
      )}
      {!!rows.length && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption>Captured source evidence</caption>
            <thead>
              <tr>
                <th>Event</th>
                <th>Source time</th>
                <th>Received</th>
                <th>Sequence</th>
                <th>First captured bid</th>
                <th>Quality flags</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.snapshot_id}>
                  <td>{row.normalized.kind}</td>
                  <td>{row.event_at ?? "Unknown"}</td>
                  <td>{row.received_at}</td>
                  <td>{row.sequence ?? "Unknown"}</td>
                  <td>{row.normalized.bids?.[0]?.price ?? "Unknown"}</td>
                  <td>{row.quality.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
