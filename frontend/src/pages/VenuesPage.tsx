/**
 * Cross-venue signals page: adapter health plus the questions more
 * than one venue lists, with the yes-price gap between them.
 *
 * Signal-only and read-only. The banner is permanent: a mismatch is a
 * comparison, never an instruction, and nothing on this page can place
 * or confirm an order. All data comes from the cached rows the backend
 * refresh loop writes, so an adapter that has never fetched is a
 * data-quality warning, not an error.
 *
 * Polls the cache once a minute — the backend itself refreshes every
 * five minutes.
 *
 * @module pages/VenuesPage
 */

import { useCallback, useEffect, useState } from "react";
import VenueMismatchTable from "../components/VenueMismatchTable";
import {
  CrossVenueGroup,
  IntraPolymarketSignal,
  VenueAdapterHealth,
  VenueMarketRow,
  VenueMismatch,
  venueService,
} from "../services/venueService";

const POLL_INTERVAL_MS = 60000;
/** Below this price gap a pair is not worth a reviewer's attention. */
const MIN_EDGE = 0.03;

function pct(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function money(value: number | null | undefined): string {
  if (value == null) return "—";
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function age(iso: string | null): string {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  return `${Math.floor(seconds / 60)}m ago`;
}

export default function VenuesPage() {
  const [adapters, setAdapters] = useState<VenueAdapterHealth[]>([]);
  const [groups, setGroups] = useState<CrossVenueGroup[]>([]);
  const [mismatches, setMismatches] = useState<VenueMismatch[]>([]);
  const [intraPolymarket, setIntraPolymarket] = useState<IntraPolymarketSignal[]>([]);
  const [markets, setMarkets] = useState<VenueMarketRow[]>([]);
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (question: string) => {
    try {
      const [adapterResponse, crossResponse, mismatchResponse, marketResponse] = await Promise.all([
        venueService.getAdapters(),
        venueService.getCrossVenueMarkets({ q: question || "the", limit: 50 }),
        venueService.getMismatches({ min_edge: MIN_EDGE, limit: 50 }),
        venueService.listMarkets({ q: question || undefined, limit: 100 }),
      ]);
      setAdapters(adapterResponse.adapters);
      setGroups(crossResponse.groups);
      setMismatches(mismatchResponse.mismatches);
      setIntraPolymarket(mismatchResponse.intra_polymarket ?? []);
      setMarkets(marketResponse.markets);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load venue data");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(search).finally(() => undefined);
  }, [load, search]);

  useEffect(() => {
    const timer = setInterval(() => load(search), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [load, search]);

  const enabledAdapters = adapters.filter((adapter) => adapter.enabled);
  const neverFetched = enabledAdapters.filter((adapter) => !adapter.last_fetch_at);
  const failedFetches = adapters.filter((adapter) => adapter.last_error);
  const playMoneyVenues = enabledAdapters.filter((adapter) => adapter.name === "manifold");
  const realMoneyVenues = enabledAdapters.filter((adapter) => adapter.name !== "manifold");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Cross-Venue Signals</h1>
        <p className="text-soft text-sm mt-1">
          The same question priced on more than one prediction venue. Kalshi (US-regulated, public
          API, real money), Manifold (play money — sentiment only) and PredictIt (real money, the
          most legally constrained) each ship off by default and stay read-only.
        </p>
      </div>

      <div className="rounded border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-400">
        <strong className="font-semibold">Signal-only.</strong> These rows are comparison data, not
        executable quotes. Nothing on this page places, modifies or confirms an order — any trade
        stays manual, on the venue itself, after you have confirmed the match and read both
        questions verbatim.
      </div>

      {playMoneyVenues.length > 0 && (
        <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-4 text-sm text-soft">
          <strong className="font-semibold text-white">Play money.</strong>{" "}
          {playMoneyVenues.map((adapter) => adapter.name).join(", ")} is a play-money venue, so its
          prices are excluded from executable signals by default. The mismatches endpoint takes{" "}
          <span className="font-mono text-xs">include_play_money</span> to show those pairs, always
          flagged as sentiment only.
        </div>
      )}

      {realMoneyVenues.length === 0 && (
        <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-4 text-sm text-soft">
          No real-money venue is enabled, so no executable signal exists: an enabled play-money
          adapter alone yields sentiment rows.
        </div>
      )}

      {error && (
        <div className="rounded border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-400">
          {error}
        </div>
      )}

      {neverFetched.length > 0 && (
        <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-4 text-sm text-soft">
          <strong className="font-semibold text-white">Data quality.</strong>{" "}
          {neverFetched.map((adapter) => adapter.name).join(", ")}{" "}
          {neverFetched.length === 1 ? "is" : "are"} enabled but has never fetched — its rows below
          are stale or absent, so any comparison against it is incomplete.
        </div>
      )}

      {failedFetches.length > 0 && (
        <div className="rounded border border-red-500/40 bg-red-500/10 p-3 text-sm text-red-400">
          Last refresh failed for: {failedFetches.map((adapter) => adapter.name).join(", ")}. Cached
          rows are from the last successful fetch.
        </div>
      )}

      <section className="surface-panel p-5 space-y-4">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-lg font-semibold">
            Adapters <span className="text-sm font-normal text-soft">({adapters.length})</span>
          </h2>
          <div className="flex items-center gap-2">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") setSearch(query.trim());
              }}
              placeholder="Filter by question…"
              className="input-field text-sm py-1.5 max-w-[16rem]"
              aria-label="Filter venues by question"
            />
            <button
              onClick={() => setSearch(query.trim())}
              className="btn-muted text-xs px-3 py-1.5"
            >
              Search
            </button>
          </div>
        </div>

        {adapters.length === 0 ? (
          <p className="text-soft text-sm">No venue adapter is registered.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-theme text-sm w-full">
              <thead>
                <tr className="text-left text-xs text-muted/60">
                  <th className="pb-2 pr-4">Venue</th>
                  <th className="pb-2 pr-4">State</th>
                  <th className="pb-2 pr-4 text-right">Markets</th>
                  <th className="pb-2 pr-4">Last fetch</th>
                  <th className="pb-2">Last error</th>
                </tr>
              </thead>
              <tbody>
                {adapters.map((adapter) => (
                  <tr key={adapter.name} className="border-t border-[var(--line)] transition">
                    <td className="py-2.5 pr-4 font-semibold">{adapter.name}</td>
                    <td className="py-2.5 pr-4">
                      <span
                        className={
                          "text-xs font-semibold px-2 py-0.5 rounded " +
                          (adapter.enabled
                            ? "bg-green-500/10 text-green-400"
                            : "bg-[var(--bg-soft)] text-soft")
                        }
                      >
                        {adapter.enabled ? "enabled" : "off by default"}
                      </span>
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">{adapter.markets}</td>
                    <td className="py-2.5 pr-4 text-xs text-soft">{age(adapter.last_fetch_at)}</td>
                    <td className="py-2.5 text-xs text-red-400">{adapter.last_error ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="text-xs text-soft">
          Adapters ship off by default per venue and refresh every five minutes; enabling one only
          reads that venue's public API. PredictIt's endpoint can answer a plain client with a 403 —
          that is a dark venue, never one worked around.
        </p>
      </section>

      <section className="surface-panel p-5 space-y-4">
        <h2 className="text-lg font-semibold">
          Mismatches <span className="text-sm font-normal text-soft">({mismatches.length})</span>
        </h2>
        <VenueMismatchTable
          mismatches={mismatches}
          groups={groups}
          adapters={adapters}
          intraPolymarket={intraPolymarket}
          loading={loading}
        />
      </section>

      <section className="surface-panel p-5 space-y-4">
        <h2 className="text-lg font-semibold">
          Cached markets <span className="text-sm font-normal text-soft">({markets.length})</span>
        </h2>
        {markets.length === 0 ? (
          <p className="text-soft text-sm">No cached rows for this filter.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="table-theme text-sm w-full">
              <thead>
                <tr className="text-left text-xs text-muted/60">
                  <th className="pb-2 pr-4">Venue</th>
                  <th className="pb-2 pr-4">Question</th>
                  <th className="pb-2 pr-4 text-right">Yes</th>
                  <th className="pb-2 pr-4 text-right">No</th>
                  <th className="pb-2 pr-4 text-right">Volume 24h</th>
                  <th className="pb-2 text-right">Fetched</th>
                </tr>
              </thead>
              <tbody>
                {markets.map((row) => (
                  <tr
                    key={`${row.venue}-${row.market_id}`}
                    className="border-t border-[var(--line)] transition"
                  >
                    <td className="py-2.5 pr-4 font-semibold">{row.venue}</td>
                    <td className="py-2.5 pr-4">
                      <a
                        href={row.url}
                        target="_blank"
                        rel="noreferrer noopener"
                        className="hover:text-[var(--accent)] transition"
                      >
                        {row.question}
                      </a>
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {pct(row.yes_price)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {pct(row.no_price)}
                    </td>
                    <td className="py-2.5 pr-4 text-right font-mono text-xs">
                      {money(row.volume_24h)}
                    </td>
                    <td className="py-2.5 text-right text-xs text-soft">{age(row.fetched_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
