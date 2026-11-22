import { useEffect, useRef, useState } from "react";
import { onActiveAccountChange } from "../services/accountService";
import { RedemptionIntent, redemptionService } from "../services/redemptionService";
import { getApiErrorMessage } from "../utils/apiError";

const states: Record<string, string> = {
  preview: "Preview — not broadcast",
  ready: "Prepared — not broadcast",
  preparing: "Preparing — not broadcast",
  submitted: "Submitted — awaiting finalized receipt",
  unknown: "Submission outcome unknown — reconciliation required",
  confirmed: "Finalized receipt verified",
  failed: "Failed — cash not recognized",
  reorged: "Receipt invalidated — review required",
};

export default function RedemptionPanel() {
  const [rows, setRows] = useState<RedemptionIntent[]>([]);
  const [condition, setCondition] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const sequence = useRef(0);
  const requestKey = useRef(crypto.randomUUID());

  useEffect(() => {
    alive.current = true;
    function load() {
      const current = ++sequence.current;
      setRows([]);
      setCondition("");
      setError(null);
      setBusy(false);
      requestKey.current = crypto.randomUUID();
      void redemptionService
        .list()
        .then((result) => {
          if (alive.current && sequence.current === current) setRows(result);
        })
        .catch((cause: unknown) => {
          if (alive.current && sequence.current === current) setError(getApiErrorMessage(cause));
        });
    }
    load();
    const unsubscribe = onActiveAccountChange(load);
    return () => {
      alive.current = false;
      unsubscribe();
    };
  }, []);

  async function update(operation: () => Promise<RedemptionIntent>) {
    const current = sequence.current;
    setBusy(true);
    setError(null);
    try {
      await operation();
      if (!alive.current || sequence.current !== current) return;
      const latest = await redemptionService.list();
      if (alive.current && sequence.current === current) setRows(latest);
    } catch (cause) {
      if (alive.current && sequence.current === current) setError(getApiErrorMessage(cause));
    } finally {
      if (alive.current && sequence.current === current) setBusy(false);
    }
  }

  async function download(row: RedemptionIntent) {
    const current = sequence.current;
    try {
      const body = await redemptionService.evidence(row.intent_id);
      if (!alive.current || current !== sequence.current) return;
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(body, null, 2)], { type: "application/json" }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = `redemption-${row.intent_id.slice(0, 12)}.json`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      if (alive.current && current === sequence.current) setError(getApiErrorMessage(cause));
    }
  }

  return (
    <section aria-label="Redemption evidence" className="surface-panel p-5 space-y-3">
      <h3 className="font-semibold">Redemption previews and receipts</h3>
      <p className="text-soft text-sm">
        Previews observe resolved CTF positions without signing or broadcasting. Expected payout is
        not available cash. Unsupported wallet or position types require manual review.
      </p>
      {error && <p role="alert">{error}</p>}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void update(() => redemptionService.preview(condition, requestKey.current));
        }}
        className="flex flex-wrap gap-3"
      >
        <label>
          Resolved condition ID
          <input
            value={condition}
            disabled={busy}
            required
            pattern="0x[a-fA-F0-9]{64}"
            className="block bg-surface border border-border"
            onChange={(event) => {
              setCondition(event.currentTarget.value);
              requestKey.current = crypto.randomUUID();
            }}
          />
        </label>
        <button className="btn-secondary" disabled={busy} type="submit">
          Preview redemption
        </button>
      </form>
      {busy && <p role="status">Reading redemption evidence…</p>}
      {!rows.length && !busy && <p>No saved redemption intents for this account.</p>}
      {rows.map((row) => (
        <article key={row.intent_id} className="border border-border rounded p-3 space-y-2">
          <p className="font-semibold">{states[row.status] ?? "Unverified redemption state"}</p>
          <p className="break-all text-sm">Condition: {row.market_id}</p>
          <p>
            Expected payout: {row.expected_payout} pUSD · Finalized payout evidence:{" "}
            {row.finalized_payout ?? "Unknown"}
          </p>
          {row.error && <p>Evidence status: {row.error}</p>}
          {row.tx_hash && /^0x[a-fA-F0-9]{64}$/.test(row.tx_hash) && (
            <a
              href={`https://polygonscan.com/tx/${row.tx_hash}`}
              target="_blank"
              rel="noopener noreferrer"
            >
              {row.on_chain_submitted
                ? "View submitted transaction"
                : "View locally computed hash (broadcast unverified)"}
            </a>
          )}
          {!row.preview_only && row.tx_hash && (
            <button
              className="btn-secondary"
              disabled={busy}
              onClick={() => void update(() => redemptionService.reconcile(row.intent_id))}
            >
              Check finalized receipt
            </button>
          )}
          <button className="btn-secondary" onClick={() => void download(row)}>
            Download redemption evidence
          </button>
        </article>
      ))}
    </section>
  );
}
