import { useEffect, useRef, useState } from "react";
import { apiClient } from "../services/apiClient";
import { onActiveAccountChange } from "../services/accountService";
import { getApiErrorMessage } from "../utils/apiError";

type Intent = {
  id: string;
  token_id: string;
  side: string;
  limit_price: string;
  notional_usdc: string;
  state: string;
  updated_at: string;
  cumulative_filled?: string;
  executed_notional_usdc?: string;
  reserved_cash_usdc?: string;
  reserved_shares?: string;
  active_from?: string;
  expires_at?: string;
  source_snapshot_id?: string;
  source_as_of?: string | null;
  source_available_at?: string | null;
  policy?: string;
  confidence?: string;
  queue_ahead?: string | null;
};
export default function PaperMakerIntents({ configId }: { configId: number }) {
  const generation = useRef(0);
  const [intents, setIntents] = useState<Intent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [summary, setSummary] = useState<{ realized_pnl_usdc?: string; fees_usdc?: string } | null>(
    null,
  );
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const invalidate = () => {
      generation.current++;
    };
    const clear = () => {
      invalidate();
      setIntents(null);
      setSummary(null);
      setError(null);
      setBusy(false);
    };
    const unsubscribe = onActiveAccountChange(clear);
    return () => {
      invalidate();
      unsubscribe();
    };
  }, [configId]);
  const load = async () => {
    const version = generation.current;
    setBusy(true);
    setError(null);
    try {
      const result = await apiClient.get<{
        intents: Intent[];
        realized_pnl_usdc?: string;
        fees_usdc?: string;
      }>(`/api/market-maker/configs/${configId}/paper-intents`);
      if (version === generation.current) {
        setIntents(result.intents);
        setSummary(result);
      }
    } catch (e) {
      if (version === generation.current)
        setError(getApiErrorMessage(e, "Could not load paper intents"));
    } finally {
      if (version === generation.current) setBusy(false);
    }
  };
  return (
    <div className="surface-soft p-3 rounded-lg text-sm space-y-2">
      <p className="text-soft">
        Paper quotes require attributed trade flow, queue and fee assumptions before modeled fills.
        The lifecycle model reserves cash or inventory; legacy quote definitions reserve neither.
        Historical paper fills are legacy_unverified_fill and excluded from decision-grade results.
        Live execution is separate. Realized results come from the shared journal; unrealized value
        requires fresh captured marks.
      </p>
      <button className="btn-muted text-xs" disabled={busy} onClick={() => void load()}>
        {busy ? "Loading intents…" : "Inspect paper quote intents"}
      </button>
      {error && (
        <p role="alert" className="text-red-400">
          {error}
        </p>
      )}
      {summary?.realized_pnl_usdc !== undefined && (
        <p>
          Realized P&amp;L (latest 100 orders) ${summary.realized_pnl_usdc} · fees ${summary.fees_usdc}. Unrealized
          P&amp;L uses shared account NAV and fresh marks.
        </p>
      )}
      {intents && (
        <ul className="space-y-2" aria-label="Paper quote intents">
          {intents.length === 0 && <li>No captured paper intents.</li>}
          {intents.map((intent) => (
            <li key={intent.id} className="text-xs break-all">
              <strong>{intent.state}</strong> · {intent.side} {intent.token_id} · limit{" "}
              {intent.limit_price} · intended ${intent.notional_usdc} USDC
              <br />
              Quote {intent.id} · updated {intent.updated_at}
              {intent.cumulative_filled !== undefined && (
                <>
                  <br />
                  Filled shares {intent.cumulative_filled} · executed $
                  {intent.executed_notional_usdc} · reserved cash ${intent.reserved_cash_usdc} ·
                  reserved shares {intent.reserved_shares}
                  <br />
                  Eligible from {intent.active_from} · expires {intent.expires_at}
                  <br />
                  {intent.policy} · {intent.confidence} · queue ahead{" "}
                  {intent.queue_ahead ?? "unknown"}
                  <br />
                  Source as of {intent.source_as_of ?? "unknown"} · available{" "}
                  {intent.source_available_at ?? "unknown"}
                  <br />
                  Source {intent.source_snapshot_id}
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
