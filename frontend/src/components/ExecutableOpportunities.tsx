/**
 * Executable opportunities: edge after VWAP, fees and slippage at a chosen size.
 * Indicative rows come from stored AI verdicts without an interval or calibration; only
 * VERIFIED rows have both. Never labelled as guaranteed profit. Research only, no order.
 *
 * @module components/ExecutableOpportunities
 */
import { useEffect, useMemo, useState } from "react";
import { getApiErrorMessage } from "../utils/apiError";
import {
  ExecutableOpportunity,
  OpportunitiesResponse,
  opportunitiesService,
} from "../services/opportunitiesService";

type Sort = "net_ev" | "per_dollar" | "conservative";

function n(value: string | null, digits = 4): string {
  if (value == null) return "n/a";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed.toFixed(digits) : "n/a";
}

const BADGE: Record<string, string> = {
  VERIFIED: "text-emerald-300",
  INDICATIVE: "text-amber-300",
  STALE: "text-soft",
  INELIGIBLE: "text-soft",
};

export default function ExecutableOpportunities() {
  const [shares, setShares] = useState("10");
  const [minEdge, setMinEdge] = useState("");
  const [maxAge, setMaxAge] = useState(3600);
  const [feeBps, setFeeBps] = useState("");
  const [sort, setSort] = useState<Sort>("net_ev");
  const [data, setData] = useState<OpportunitiesResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    opportunitiesService
      .list({
        strategy: "ai_forecast",
        shares,
        max_age: maxAge,
        ...(minEdge ? { min_edge: minEdge } : {}),
        ...(feeBps ? { fee_bps: feeBps, fee_version: "user-entered" } : {}),
      })
      .then((response) => {
        if (!cancelled) {
          setData(response);
          setError(null);
        }
      })
      .catch((err) => !cancelled && setError(getApiErrorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [shares, minEdge, maxAge, feeBps]);

  const rows = useMemo(() => {
    const list: ExecutableOpportunity[] = [...(data?.ranked ?? [])];
    const key = (o: ExecutableOpportunity) =>
      Number(
        sort === "net_ev"
          ? o.net_ev
          : sort === "per_dollar"
            ? o.net_ev_per_dollar
            : (o.conservative_net_ev ?? -Infinity),
      );
    return list.sort((a, b) => key(b) - key(a));
  }, [data, sort]);

  return (
    <section className="surface-panel p-4 space-y-3" data-testid="executable-opportunities">
      <h3 className="text-white font-semibold">Executable opportunities (estimate)</h3>
      <p className="text-xs text-soft">
        Indicative, not guaranteed profit. Research only, no order is placed. Edges use displayed
        order-book depth at your size and expire with the captured book.
      </p>
      <div className="flex flex-wrap gap-2 text-xs">
        <label>
          Shares{" "}
          <input value={shares} onChange={(e) => setShares(e.target.value)} className="w-20" />
        </label>
        <label>
          Min edge{" "}
          <input value={minEdge} onChange={(e) => setMinEdge(e.target.value)} className="w-20" />
        </label>
        <label>
          Fee bps{" "}
          <input value={feeBps} onChange={(e) => setFeeBps(e.target.value)} className="w-20" />
        </label>
        <label>
          Max forecast age{" "}
          <select value={maxAge} onChange={(e) => setMaxAge(Number(e.target.value))}>
            <option value={900}>15 min</option>
            <option value={3600}>1 h</option>
            <option value={86400}>24 h</option>
          </select>
        </label>
        <label>
          Sort{" "}
          <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
            <option value="net_ev">Net EV</option>
            <option value="per_dollar">Net EV per $</option>
            <option value="conservative">Conservative EV</option>
          </select>
        </label>
      </div>
      {error && (
        <p className="text-xs text-soft">
          Opportunities are unavailable ({error}). They require the executable-opportunities flag.
        </p>
      )}
      {data && rows.length === 0 && !error && (
        <p className="text-xs text-soft">No opportunity has a positive edge after costs.</p>
      )}
      <ul className="space-y-2">
        {rows.map((o) => (
          <li
            key={`${o.forecast_id}-${o.outcome}`}
            className="text-xs border-b border-white/5 pb-2"
          >
            <div className="flex justify-between">
              <span className="text-white">
                {o.market_id.slice(0, 10)}… buy {o.outcome} x {n(o.size, 2)} @ VWAP{" "}
                {n(o.executable_vwap)}
              </span>
              <span className={BADGE[o.status]} data-testid="opportunity-status">
                {o.status}
              </span>
            </div>
            <div className="text-soft">
              Net EV {n(o.net_ev)} ({n(o.net_ev_per_dollar)} per $) · conservative{" "}
              {n(o.conservative_net_ev)} · fees {n(o.fees)} · slippage {n(o.slippage)} · capital{" "}
              {n(o.capital_required)}
            </div>
            {o.reasons.length > 0 && (
              <div className="text-soft">Not verified: {o.reasons.join(", ")}</div>
            )}
            {o.book_age_ms != null && o.book_age_ms > 60000 && (
              <div className="text-amber-300">Stale book ({Math.round(o.book_age_ms / 1000)}s)</div>
            )}
            <div className="text-soft">Not modeled: {o.unmodeled_costs.join(", ")}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}
