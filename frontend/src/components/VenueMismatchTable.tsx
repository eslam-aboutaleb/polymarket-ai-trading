/**
 * Cross-venue mismatch table: the same question priced differently on
 * two venues, plus the exact-key groups the Phase A cross route returns.
 *
 * Signal-only, and the banner says so on every render. Every row shows
 * each side's question **verbatim** next to its price, gap and
 * liquidity, so a reviewer can confirm the match instead of trusting
 * it — a fuzzy match or a stale row produces a fake edge, and that has
 * to be visible rather than inferred. A play-money leg (Manifold) is
 * called out and never presented as executable, and an adapter that
 * has never fetched raises a data-quality warning above the table.
 *
 * @module components/VenueMismatchTable
 */
import {
  CrossVenueGroup,
  IntraPolymarketSignal,
  VenueAdapterHealth,
  VenueMismatch,
  VenueMatchType,
} from "../services/venueService";

interface VenueMismatchTableProps {
  mismatches: VenueMismatch[];
  groups: CrossVenueGroup[];
  adapters: VenueAdapterHealth[];
  /** Intra-Polymarket spots read from the existing detector. */
  intraPolymarket: IntraPolymarketSignal[];
  loading: boolean;
}

function pct(value: number | null | undefined): string {
  if (value == null) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

function liquidity(value: number | null | undefined): string {
  if (value == null || value <= 0) return "—";
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function score(match: VenueMatchType, value: number): string {
  return match === "exact" ? "exact key" : `fuzzy ${(value * 100).toFixed(0)}%`;
}

/** Oldest fetched_at in a mismatch — how stale its signal is. */
function mismatchAge(mismatch: VenueMismatch): string {
  const stamps = [mismatch.cheap.fetched_at, mismatch.rich.fetched_at]
    .filter((stamp): stamp is string => stamp != null)
    .map((stamp) => new Date(stamp).getTime());
  if (stamps.length < 2) return "never fetched";
  const seconds = Math.max(0, (Date.now() - Math.min(...stamps)) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  return `${Math.floor(seconds / 60)}m ago`;
}

/** Oldest fetched_at in an exact-match group. */
function groupAge(group: CrossVenueGroup): string {
  const stamps = group.rows
    .map((row) => (row.fetched_at ? new Date(row.fetched_at).getTime() : null))
    .filter((value): value is number => value != null);
  if (stamps.length === 0) return "never fetched";
  const seconds = Math.max(0, (Date.now() - Math.min(...stamps)) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  return `${Math.floor(seconds / 60)}m ago`;
}

function SignalBanner() {
  return (
    <div className="rounded border border-amber-500/40 bg-amber-500/10 p-4 text-sm text-amber-400">
      <strong className="font-semibold">Signal-only — manual confirmation required.</strong> A gap
      between two venues is comparison data, not an executable quote: no fees, spreads or fill
      probability are modelled, and the match itself needs a human to confirm it. Nothing here
      places, modifies or confirms an order — any trade stays manual, on the venue, after you have
      checked both questions.
    </div>
  );
}

function DataQualityWarning({ adapters }: { adapters: VenueAdapterHealth[] }) {
  const neverFetched = adapters.filter((adapter) => adapter.enabled && !adapter.last_fetch_at);
  if (neverFetched.length === 0) return null;
  return (
    <div className="rounded border border-[var(--line)] bg-[var(--bg-soft)] p-4 text-sm text-soft">
      <strong className="font-semibold text-white">Data quality.</strong>{" "}
      {neverFetched.map((adapter) => adapter.name).join(", ")}{" "}
      {neverFetched.length === 1 ? "is" : "are"} enabled but has never fetched — rows for{" "}
      {neverFetched.length === 1 ? "it" : "them"} are absent, so any comparison against{" "}
      {neverFetched.length === 1 ? "it" : "them"} is incomplete. A missing venue is not a zero
      price.
    </div>
  );
}

export default function VenueMismatchTable({
  mismatches,
  groups,
  adapters,
  intraPolymarket,
  loading,
}: VenueMismatchTableProps) {
  return (
    <div className="space-y-4">
      <SignalBanner />
      <DataQualityWarning adapters={adapters} />

      {loading ? (
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="surface-panel p-4 animate-pulse h-14" />
          ))}
        </div>
      ) : mismatches.length === 0 ? (
        <p className="text-soft text-sm py-4">
          No cross-venue gap above the minimum edge yet. A mismatch needs the same question cached
          on two enabled venues, at least one of them real money.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="table-theme text-sm w-full">
            <thead>
              <tr className="text-left text-xs text-muted/60">
                <th className="pb-2 pr-4">Match</th>
                <th className="pb-2 pr-4">Cheaper side</th>
                <th className="pb-2 pr-4">Richer side</th>
                <th className="pb-2 pr-4 text-right">Gap</th>
                <th className="pb-2 pr-4 text-right">Liquidity</th>
                <th className="pb-2 pr-4 text-right">Data age</th>
                <th className="pb-2 text-right">Signal</th>
              </tr>
            </thead>
            <tbody>
              {mismatches.map((mismatch) => (
                <tr
                  key={`${mismatch.cheap.venue}-${mismatch.cheap.market_id}-${mismatch.rich.venue}-${mismatch.rich.market_id}`}
                  className="border-t border-[var(--line)] transition"
                >
                  <td className="py-2.5 pr-4 align-top">
                    <span className="text-xs font-semibold px-2 py-0.5 rounded bg-[var(--bg-soft)] text-soft">
                      {score(mismatch.match_type, mismatch.match_score)}
                    </span>
                    {mismatch.horizon_mismatch && (
                      <span
                        className="ml-2 text-xs px-2 py-0.5 rounded bg-red-500/10 text-red-400"
                        title="The two legs close more than a week apart — check that these are the same question"
                      >
                        horizon {mismatch.end_date_delta_days}d
                      </span>
                    )}
                    {mismatch.play_money_venues.length > 0 && (
                      <span
                        className="ml-2 text-xs px-2 py-0.5 rounded bg-amber-500/10 text-amber-400"
                        title={mismatch.play_money_reason ?? undefined}
                      >
                        play money
                      </span>
                    )}
                  </td>
                  <td className="py-2.5 pr-4">
                    <div className="font-semibold">{mismatch.cheap.venue}</div>
                    <a
                      href={mismatch.cheap.url || "#"}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="hover:text-[var(--accent)] transition"
                    >
                      {mismatch.cheap.question}
                    </a>
                    <div className="font-mono text-xs text-soft">
                      yes {pct(mismatch.cheap.yes_price)}
                    </div>
                  </td>
                  <td className="py-2.5 pr-4">
                    <div className="font-semibold">{mismatch.rich.venue}</div>
                    <a
                      href={mismatch.rich.url || "#"}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="hover:text-[var(--accent)] transition"
                    >
                      {mismatch.rich.question}
                    </a>
                    <div className="font-mono text-xs text-soft">
                      yes {pct(mismatch.rich.yes_price)}
                    </div>
                  </td>
                  <td className="py-2.5 pr-4 text-right font-mono text-xs font-semibold text-amber-400">
                    {pct(mismatch.edge)}
                  </td>
                  <td className="py-2.5 pr-4 text-right font-mono text-xs">
                    {liquidity(mismatch.cheap.liquidity)}
                    {" / "}
                    {liquidity(mismatch.rich.liquidity)}
                  </td>
                  <td className="py-2.5 pr-4 text-right text-xs text-soft">
                    {mismatchAge(mismatch)}
                  </td>
                  <td className="py-2.5 text-right text-xs">
                    {mismatch.executable_signal ? (
                      <span className="text-green-400">review &amp; confirm</span>
                    ) : (
                      <span className="text-amber-400">
                        {mismatch.play_money_only ? "sentiment only" : "not comparable"}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {!loading && groups.length > 0 && (
        <details className="surface-panel p-4">
          <summary className="cursor-pointer text-sm font-semibold">
            Exact-question groups <span className="text-soft font-normal">({groups.length})</span>
          </summary>
          <p className="text-xs text-soft mt-2">
            Same-level view keyed only on the normalized question, without the edge threshold. A
            group with no gap is a market the venues agree on.
          </p>
          <div className="overflow-x-auto mt-3">
            <table className="table-theme text-sm w-full">
              <thead>
                <tr className="text-left text-xs text-muted/60">
                  <th className="pb-2 pr-4">Question</th>
                  <th className="pb-2 pr-4">Venue</th>
                  <th className="pb-2 pr-4 text-right">Yes price</th>
                  <th className="pb-2 pr-4 text-right">Gap</th>
                  <th className="pb-2 pr-4 text-right">Liquidity</th>
                  <th className="pb-2 text-right">Data age</th>
                </tr>
              </thead>
              <tbody>
                {groups.map((group) =>
                  group.rows.map((row, index) => (
                    <tr
                      key={`${group.key}-${row.venue}-${row.market_id}`}
                      className="border-t border-[var(--line)] transition"
                    >
                      <td className="py-2.5 pr-4">
                        {index === 0 ? (
                          <a
                            href={row.url}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="hover:text-[var(--accent)] transition"
                          >
                            {row.question}
                          </a>
                        ) : (
                          <span className="text-muted/60">↳ same question</span>
                        )}
                        {index === 0 && (
                          <span className="ml-2 text-xs text-muted/60">
                            {row.venue} wrote: “{group.questions[row.venue] ?? row.question}”
                          </span>
                        )}
                      </td>
                      <td className="py-2.5 pr-4 font-semibold">{row.venue}</td>
                      <td className="py-2.5 pr-4 text-right font-mono text-xs">
                        {pct(row.yes_price)}
                      </td>
                      <td className="py-2.5 pr-4 text-right font-mono text-xs font-semibold text-amber-400">
                        {index === 0 ? pct(group.edge) : ""}
                      </td>
                      <td className="py-2.5 pr-4 text-right font-mono text-xs">
                        {liquidity(row.liquidity)}
                      </td>
                      <td className="py-2.5 text-right text-xs text-soft">
                        {index === 0 ? groupAge(group) : ""}
                      </td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {!loading && intraPolymarket.length > 0 && (
        <details className="surface-panel p-4">
          <summary className="cursor-pointer text-sm font-semibold">
            Intra-Polymarket spots{" "}
            <span className="text-soft font-normal">({intraPolymarket.length})</span>
          </summary>
          <p className="text-xs text-soft mt-2">
            Read from the existing Polymarket-only detector (complement and spread checks) so both
            arbitrage kinds share one surface. Same rule: review, then act manually.
          </p>
          <ul className="mt-3 space-y-1 text-sm">
            {intraPolymarket.map((spot, index) => (
              <li
                key={`${spot.market_id}-${spot.type}-${index}`}
                className="flex flex-wrap items-baseline gap-x-3 border-t border-[var(--line)] py-1.5"
              >
                <span className="text-xs px-2 py-0.5 rounded bg-[var(--bg-soft)] text-soft">
                  {spot.type}
                </span>
                <span>{spot.market_title}</span>
                <span className="font-mono text-xs text-amber-400">
                  {spot.profit_pct != null ? `${spot.profit_pct.toFixed(2)}% profit` : null}
                  {spot.spread_pct != null ? `${spot.spread_pct.toFixed(2)}% spread` : null}
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
