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
};
export default function PaperMakerIntents({ configId }: { configId: number }) {
  const generation = useRef(0);
  const [intents, setIntents] = useState<Intent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const clear = () => {
      generation.current++;
      setIntents(null);
      setError(null);
      setBusy(false);
    };
    const unsubscribe = onActiveAccountChange(clear);
    return () => {
      generation.current++;
      unsubscribe();
    };
  }, [configId]);
  const load = async () => {
    const version = generation.current;
    setBusy(true);
    setError(null);
    try {
      const result = await apiClient.get<{ intents: Intent[] }>(
        `/api/market-maker/configs/${configId}/paper-intents`,
      );
      if (version === generation.current) setIntents(result.intents);
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
        Paper quotes remain unfilled without qualifying trade flow and queue evidence. They reserve
        no cash and represent no executed volume. Historical paper counters are unverified
        estimates.
      </p>
      <button className="btn-muted text-xs" disabled={busy} onClick={() => void load()}>
        {busy ? "Loading intents…" : "Inspect paper quote intents"}
      </button>
      {error && (
        <p role="alert" className="text-red-400">
          {error}
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
              Definition {intent.id} · updated {intent.updated_at}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
