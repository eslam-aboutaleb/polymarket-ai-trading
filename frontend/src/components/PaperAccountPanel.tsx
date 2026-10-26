import { useEffect, useRef, useState } from "react";
import { PaperAccountValue, paperService } from "../services/paperService";
import { getApiErrorMessage } from "../utils/apiError";

export default function PaperAccountPanel({ onSnapshot }: { onSnapshot: () => void }) {
  const alive = useRef(true);
  const [value, setValue] = useState<PaperAccountValue | null>(null);
  const [fee, setFee] = useState("");
  const [ack, setAck] = useState(false);
  const [condition, setCondition] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    alive.current = true;
    let active = true;
    paperService
      .account()
      .then((account) => {
        if (active) {
          setValue(account);
          setFee(account.fee_bps ?? "");
        }
      })
      .catch((err: unknown) => {
        if (active) setError(getApiErrorMessage(err, "Paper accounting unavailable"));
      });
    return () => {
      active = false;
      alive.current = false;
    };
  }, []);

  async function update(operation: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await operation();
      if (!alive.current) return;
      const latest = await paperService.account();
      if (!alive.current) return;
      setValue(latest);
      window.dispatchEvent(new Event("polymarket:paper-account-updated"));
      onSnapshot();
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="surface-panel p-5 space-y-3" aria-label="Paper account controls">
      <h3 className="font-semibold">Paper account</h3>
      <p className="text-soft text-sm">
        Modeled cash and shares, separate from wallet funds. Missing fees or fresh native books
        prevent fills. Marks are indicative exit bids.
      </p>
      {error && <p role="alert">{error}</p>}
      {value && (
        <>
          {value.initialized && (
            <p>
              Current modeled NAV: ${value.nav ?? "unknown"} · Cash: ${value.cash} · Reserved: $
              {value.reserved_cash} · Modeled fees: ${value.fees}
            </p>
          )}
          {value.quality === "legacy_unverified" && (
            <p>
              Historical simulated trades cannot prove cash or inventory. Acknowledge their
              exclusion to start a separate journal account; the old records are preserved.
            </p>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void update(() => paperService.configure(fee, ack));
            }}
            className="flex flex-wrap gap-3 items-center"
          >
            <label>
              Fee assumption (bps){" "}
              <input
                aria-label="Fee assumption (bps)"
                className="input-field"
                type="number"
                min="0"
                max="10000"
                step="0.01"
                required
                value={fee}
                onChange={(event) => setFee(event.target.value)}
              />
            </label>
            {value.quality === "legacy_unverified" && (
              <label>
                <input
                  type="checkbox"
                  checked={ack}
                  onChange={(event) => setAck(event.target.checked)}
                />{" "}
                Exclude unverified historical results
              </label>
            )}
            <button
              className="btn-primary"
              disabled={busy || (value.quality === "legacy_unverified" && !ack)}
            >
              Save paper assumptions
            </button>
          </form>
          <p className="text-soft text-xs">
            This fee is your explicit model assumption, not a verified venue fee. Starting funding
            comes from your paper settings once; changing those settings cannot reset running NAV.
          </p>
          <div className="flex flex-wrap gap-3 items-center">
            <label>
              Market condition ID{" "}
              <input
                className="input-field"
                value={condition}
                placeholder="0x…"
                onChange={(event) => setCondition(event.target.value)}
              />
            </label>
            <button
              className="btn-primary"
              disabled={busy || !/^0x[0-9a-f]{64}$/i.test(condition)}
              onClick={() => void update(() => paperService.refreshMarket(condition))}
            >
              Refresh public market evidence
            </button>
            <button
              className="btn-primary"
              disabled={busy || !value.initialized}
              onClick={() => void update(() => paperService.snapshot())}
            >
              Record hourly paper NAV
            </button>
          </div>
        </>
      )}
    </section>
  );
}
