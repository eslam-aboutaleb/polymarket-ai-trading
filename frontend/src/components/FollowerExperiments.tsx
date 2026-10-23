import { getActiveAccountSelection, onActiveAccountChange } from "../services/accountService";
import { useEffect, useRef, useState } from "react";
import { apiClient } from "../services/apiClient";
import { getApiErrorMessage } from "../utils/apiError";

type ExperimentInput = {
  wallet: string;
  start_at: string;
  end_at: string;
  funding: string;
  order_notional: string;
  fee_bps: string | null;
  delays_seconds: number[];
  dataset: { source: string; signals: unknown[]; books: unknown[] };
};
type Scenario = {
  delay_seconds: number;
  valuation: {
    nav: string | null;
    net_pnl: string | null;
    cash: string;
    fees: string;
    realized_pnl: string;
    missing_marks: string[];
  };
  outcomes: { signal_id: string; status: string; reason: string; shares: string; vwap?: string }[];
  journal: unknown[];
};
type Run = {
  id: string;
  input_hash: string;
  created_at: string;
  input: ExperimentInput;
  result: {
    dataset_hash: string;
    model_version: string;
    quality?: string;
    capture_manifest_hash?: string;
    capture_manifest?: unknown[];
    scenarios: Scenario[];
    baseline: { nav: string };
  };
};
const endpoint = "/api/copy-trading/experiments";
const money = (value: string | null) =>
  value === null ? "Unknown" : `$${Number(value).toFixed(2)}`;

export default function FollowerExperiments() {
  const accountGeneration = useRef(0);
  const [accountKey, setAccountKey] = useState(() => getActiveAccountSelection()?.id ?? 0);
  useEffect(
    () =>
      onActiveAccountChange((account) => {
        accountGeneration.current++;
        setAccountKey(account?.id ?? 0);
        setInput(null);
        setRun(null);
        setRuns([]);
        setError(null);
        setBusy(false);
        setCaptured(false);
        setCaptureMessage(null);
      }),
    [],
  );
  const [input, setInput] = useState<ExperimentInput | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [runs, setRuns] = useState<Pick<Run, "id" | "created_at">[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [captured, setCaptured] = useState(false);
  const [captureMessage, setCaptureMessage] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    apiClient
      .get<{ runs: Pick<Run, "id" | "created_at">[] }>(endpoint)
      .then((data) => {
        if (active) setRuns(data.runs);
      })
      .catch((e) => {
        if (active) setError(getApiErrorMessage(e, "Could not load experiments"));
      });
    return () => {
      active = false;
    };
  }, [accountKey]);
  const importFile = async (file?: File) => {
    if (!file) return;
    const generation = accountGeneration.current;
    setError(null);
    setRun(null);
    setInput(null);
    setCaptured(false);
    setCaptureMessage(null);
    try {
      if (file.size > 256_000) throw new Error("Choose an experiment smaller than 256 KB");
      const value = JSON.parse(await file.text());
      if (generation !== accountGeneration.current) return;
      if (
        !value ||
        typeof value.wallet !== "string" ||
        !value.dataset ||
        !Array.isArray(value.dataset.signals) ||
        !Array.isArray(value.dataset.books) ||
        typeof value.start_at !== "string" ||
        typeof value.end_at !== "string" ||
        typeof value.dataset.source !== "string" ||
        (value.delays_seconds != null &&
          (!Array.isArray(value.delays_seconds) ||
            value.delays_seconds.length > 4 ||
            !value.delays_seconds.every(
              (d: unknown) => typeof d === "number" && Number.isInteger(d) && d >= 0 && d <= 86400,
            )))
      ) {
        throw new Error("Expected a frozen experiment with wallet, times, signals and books");
      }
      setInput({
        ...value,
        funding: String(value.funding ?? 1000),
        order_notional: String(value.order_notional ?? 10),
        fee_bps: value.fee_bps == null ? null : String(value.fee_bps),
        delays_seconds: value.delays_seconds ?? [0, 60, 300, 1800],
      });
    } catch (e) {
      if (generation === accountGeneration.current)
        setError(e instanceof Error ? e.message : "Could not import experiment");
    }
  };
  const submit = async () => {
    if (!input) return;
    const generation = accountGeneration.current;
    setBusy(true);
    setError(null);
    setRun(null);
    try {
      const saved = await apiClient.post<Run>(
        captured ? `${endpoint}/from-capture` : endpoint,
        input,
      );
      if (generation !== accountGeneration.current) return;
      setRun(saved);
      setInput(saved.input);
      setRuns((previous) => [saved, ...previous].slice(0, 20));
    } catch (e) {
      if (generation === accountGeneration.current)
        setError(getApiErrorMessage(e, "Could not save experiment. Check the input contract."));
    } finally {
      if (generation === accountGeneration.current) setBusy(false);
    }
  };
  const load = async (id: string) => {
    const generation = accountGeneration.current;
    setBusy(true);
    setError(null);
    setRun(null);
    try {
      const saved = await apiClient.get<Run>(`${endpoint}/${id}`);
      if (generation !== accountGeneration.current) return;
      setRun(saved);
      setInput(saved.input);
      setCaptured(saved.result.quality === "native_captured_modeled");
    } catch (e) {
      if (generation === accountGeneration.current)
        setError(getApiErrorMessage(e, "Could not load experiment"));
    } finally {
      if (generation === accountGeneration.current) setBusy(false);
    }
  };
  return (
    <section className="surface-panel rounded-xl p-5 space-y-4" aria-label="Follower experiments">
      <h2 className="text-xl font-semibold">Simulate following a trader</h2>
      <p className="text-soft text-sm">
        Compare delays using a frozen dataset you supply or capture from public venue endpoints.
        Fills and fee policies remain modeled research assumptions. Leader returns and your
        watchlist are never used as follower profits. Live following is disabled.
      </p>
      <p className="text-soft text-sm">
        Each signal buys or sells up to the chosen USDC notional, capped by cash, owned shares and
        supplied depth. Missing fees or eligible books leave signals unfilled. Open positions
        without a fresh final bid have unknown NAV.
      </p>
      <p className="text-soft text-sm">
        Native capture starts at receipt time and does not backfill earlier books. Each request
        checks up to 50 recent trade records across at most five markets. Closed or inactive markets
        need verified settlement evidence and cannot be replayed here. Capture again later to
        collect books for delayed scenarios.
      </p>
      <div className="flex flex-wrap gap-4 items-center">
        <button
          className="btn-muted"
          disabled={busy}
          onClick={() => {
            setRun(null);
            setCaptured(true);
            setCaptureMessage(null);
            setInput({
              wallet: "",
              start_at: new Date(Date.now() - 86400000).toISOString(),
              end_at: new Date().toISOString(),
              funding: "1000",
              order_notional: "10",
              fee_bps: null,
              delays_seconds: [0, 60, 300, 1800],
              dataset: { source: "native receipt-time capture", signals: [], books: [] },
            });
          }}
        >
          Use native captured data
        </button>
        <label className="text-sm">
          Import frozen experiment JSON
          <input
            aria-label="Import frozen experiment JSON"
            type="file"
            accept=".json,application/json"
            disabled={busy}
            className="block mt-2"
            onChange={(e) => void importFile(e.target.files?.[0])}
          />
        </label>
        <a
          className="text-[var(--accent)] underline text-sm"
          href="/examples/follower-experiment.json"
          download
        >
          Download synthetic example
        </a>
      </div>
      {captureMessage && (
        <p role="status" className="text-sm text-soft">
          {captureMessage}
        </p>
      )}
      {error && (
        <p role="alert" className="text-red-400">
          {error}
        </p>
      )}
      {input && (
        <div className="space-y-3">
          <p className="text-xs text-soft">
            Source ({captured ? "server captured" : "user supplied"}): {input.dataset.source} ·{" "}
            {input.dataset.signals.length} signals · {input.dataset.books.length} books
          </p>
          <div className="grid sm:grid-cols-2 gap-3">
            {(
              [
                ["wallet", "Trader wallet"],
                ["start_at", "Follow start (ISO timestamp with timezone)"],
                ["end_at", "End (ISO timestamp with timezone)"],
                ["funding", "Initial funding (USDC)"],
                ["order_notional", "Per-signal notional (USDC)"],
              ] as const
            ).map(([key, label]) => (
              <label key={key} className="text-sm">
                {label}
                <input
                  className="input-field w-full mt-1"
                  value={input[key]}
                  disabled={busy}
                  onChange={(e) => {
                    setRun(null);
                    setInput({ ...input, [key]: e.target.value });
                  }}
                />
              </label>
            ))}
            <label className="text-sm">
              Assumed fee (basis points; blank means unknown)
              <input
                className="input-field w-full mt-1"
                value={input.fee_bps ?? ""}
                disabled={busy}
                onChange={(e) => {
                  setRun(null);
                  setInput({ ...input, fee_bps: e.target.value || null });
                }}
              />
            </label>
          </div>
          {captured && (
            <button
              className="btn-muted"
              disabled={busy || !/^0x[0-9a-fA-F]{40}$/.test(input.wallet)}
              onClick={async () => {
                const generation = accountGeneration.current;
                setBusy(true);
                setRun(null);
                setCaptureMessage(null);
                setError(null);
                try {
                  const data = await apiClient.post<{
                    books_captured: number;
                    failures: string[];
                    captured_at: string;
                  }>("/api/replay/capture", { wallet: input.wallet });
                  if (generation !== accountGeneration.current) return;
                  setInput({ ...input, end_at: data.captured_at });
                  setCaptureMessage(
                    `Captured ${data.books_captured} books. Earlier history cannot be reconstructed.`,
                  );
                  if (!data.books_captured || data.failures.length)
                    setError(
                      `Captured ${data.books_captured} books; ${data.failures.length} evidence gaps. Earlier history cannot be reconstructed.`,
                    );
                } catch (e) {
                  if (generation === accountGeneration.current)
                    setError(getApiErrorMessage(e, "Native capture unavailable"));
                } finally {
                  if (generation === accountGeneration.current) setBusy(false);
                }
              }}
            >
              Capture trader and native books now
            </button>
          )}
          <p className="text-xs text-soft">
            Delay scenarios: {input.delays_seconds.join(", ")} seconds. Fees, latency, tick/lot
            rules, timestamps and depth are assumptions from this input, not independently verified
            venue observations.
          </p>
          <button className="btn-accent" disabled={busy} onClick={() => void submit()}>
            {busy ? "Saving experiment…" : "Run and save simulation"}
          </button>
        </div>
      )}
      {run && (
        <div className="space-y-3" data-testid="follower-result">
          <h3 className="font-semibold">
            Saved research comparison ·{" "}
            {run.result.quality === "native_captured_modeled"
              ? "native captures, modeled fills"
              : "unverified inputs"}
          </h3>
          <p className="text-xs text-soft break-all">
            No-copy baseline: {money(run.result.baseline.nav)} cash · {run.result.model_version}
            <br />
            Input hash: {run.input_hash}
            <br />
            Dataset: {run.result.dataset_hash}
          </p>
          {run.result.capture_manifest_hash && (
            <details className="surface-soft rounded p-3">
              <summary>Captured source manifest</summary>
              <p className="text-xs text-soft break-all">
                SHA256: {run.result.capture_manifest_hash}
              </p>
              <a
                className="text-[var(--accent)] underline text-sm"
                href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(run.result.capture_manifest, null, 2))}`}
                download="captured-source-manifest.json"
              >
                Download source manifest
              </a>
            </details>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr>
                  <th>Delay</th>
                  <th>NAV</th>
                  <th>Net P&amp;L</th>
                  <th>Fees</th>
                  <th>Cash</th>
                </tr>
              </thead>
              <tbody>
                {run.result.scenarios.map((s) => (
                  <tr key={s.delay_seconds}>
                    <td>{s.delay_seconds}s</td>
                    <td>{money(s.valuation.nav)}</td>
                    <td>{money(s.valuation.net_pnl)}</td>
                    <td>{money(s.valuation.fees)}</td>
                    <td>{money(s.valuation.cash)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {run.result.scenarios.map((s) => (
            <details key={s.delay_seconds} className="surface-soft rounded p-3">
              <summary>{s.delay_seconds}s: signal outcomes and accounting journal</summary>
              <p className="text-xs text-soft">
                Missing final marks: {s.valuation.missing_marks.join(", ") || "none"} ·{" "}
                {s.journal.length} journal events · Sharpe unavailable (no aligned periodic NAV
                series)
              </p>
              <ul className="text-sm">
                {s.outcomes.map((o) => (
                  <li key={o.signal_id}>
                    {o.signal_id}: {o.status} · {o.reason} · {o.shares} shares
                    {o.vwap ? ` @ ${o.vwap}` : ""}
                  </li>
                ))}
              </ul>
              <a
                className="text-[var(--accent)] underline text-sm"
                href={`data:application/json;charset=utf-8,${encodeURIComponent(JSON.stringify(s.journal, null, 2))}`}
                download={`journal-${s.delay_seconds}s.json`}
              >
                Download journal
              </a>
            </details>
          ))}
        </div>
      )}
      {runs.length > 0 && (
        <details>
          <summary>Saved experiments in this account</summary>
          <ul className="space-y-2 mt-2">
            {runs.map((row) => (
              <li key={row.id}>
                <button
                  className="btn-muted text-xs"
                  disabled={busy}
                  onClick={() => void load(row.id)}
                >
                  {row.created_at} · {row.id}
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
