import { useEffect, useRef, useState } from "react";
import { AttributionStudy, executionAnalyticsService } from "../services/executionAnalyticsService";
import { getApiErrorMessage } from "../utils/apiError";

const localTime = (days: number) => {
  const date = new Date(Date.now() + days * 86400000);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};

export default function ForwardAttributionStudy({ onSelect }: { onSelect: (id: string) => void }) {
  const alive = useRef(true);
  const [studies, setStudies] = useState<AttributionStudy[]>([]);
  const [selected, setSelected] = useState("");
  const [strategy, setStrategy] = useState("manual");
  const [end, setEnd] = useState(() => localTime(1));
  const [deadline, setDeadline] = useState(() => localTime(7));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    alive.current = true;
    void executionAnalyticsService
      .getAttributionStudies()
      .then((rows) => {
        if (active) setStudies(rows);
      })
      .catch((cause) => {
        if (active) setError(getApiErrorMessage(cause));
      });
    return () => {
      active = false;
      alive.current = false;
    };
  }, []);
  async function register() {
    setBusy(true);
    setError(null);
    try {
      const row = await executionAnalyticsService.registerAttributionStudy({
        strategy,
        enrollment_end: new Date(end).toISOString(),
        settlement_deadline: new Date(deadline).toISOString(),
      });
      if (!alive.current) return;
      setStudies((rows) => [row, ...rows].slice(0, 20));
      setSelected(row.study_id);
      onSelect(row.study_id);
    } catch (cause) {
      if (alive.current) setError(getApiErrorMessage(cause));
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  const policy = studies.find((row) => row.study_id === selected);
  return (
    <details className="border-t border-border py-3">
      <summary>Prospective benchmark study</summary>
      <p className="text-soft text-sm">
        Freeze a strategy and future evaluation window. Every eligible entry is retained;
        uncertainty is measured across native event groups. Capture finalized observations at least
        every 12 hours and retain native books before decisions. Results distinguish observed
        trading from a modeled market control.
      </p>
      <label className="block">
        Study strategy
        <select
          aria-label="Study strategy"
          value={strategy}
          onChange={(event) => setStrategy(event.currentTarget.value)}
          className="block bg-surface border border-border"
        >
          {["manual", "copy", "market_maker", "inverse", "latency_arb"].map((name) => (
            <option key={name} value={name}>
              {name.replace(/_/g, " ")}
            </option>
          ))}
        </select>
      </label>
      <label className="block">
        Entry enrollment ends (local time)
        <input
          aria-label="Entry enrollment ends"
          type="datetime-local"
          value={end}
          onChange={(event) => setEnd(event.currentTarget.value)}
          className="block bg-surface border border-border"
        />
      </label>
      <label className="block">
        Settlement observation deadline (local time)
        <input
          aria-label="Settlement observation deadline"
          type="datetime-local"
          value={deadline}
          onChange={(event) => setDeadline(event.currentTarget.value)}
          className="block bg-surface border border-border"
        />
      </label>
      <button
        className="btn-secondary my-2"
        disabled={busy || !end || !deadline}
        onClick={() => void register()}
      >
        Register future study
      </button>
      <label className="block">
        Study for saved evidence
        <select
          aria-label="Study for saved evidence"
          value={selected}
          className="block bg-surface border border-border"
          onChange={(event) => {
            setSelected(event.currentTarget.value);
            onSelect(event.currentTarget.value);
          }}
        >
          <option value="">No prospective study attached</option>
          {studies.map((row) => (
            <option key={row.study_id} value={row.study_id}>
              {row.strategy} — {new Date(row.registered_at).toLocaleString()}
            </option>
          ))}
        </select>
      </label>
      {policy && (
        <p>
          {policy.minimum_events} event groups required. Entries through{" "}
          {new Date(policy.enrollment_end).toLocaleString()}; settlement observations through{" "}
          {new Date(policy.settlement_deadline).toLocaleString()}.
        </p>
      )}
      {error && (
        <p role="alert" className="text-red-400">
          {error}
        </p>
      )}
    </details>
  );
}
