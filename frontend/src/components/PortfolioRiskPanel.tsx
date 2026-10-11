/**
 * Portfolio risk panel: deterministic concentration, loss bound, clusters and catalysts.
 *
 * Fetches `GET /api/portfolio/risk-report` on mount with a manual
 * `useEffect` + `useCallback` fetch (the repo convention — no TanStack Query
 * here). Every figure except the AI narrative is computed server-side, so the
 * panel states plainly that the loss bound is a bound and the VaR is an
 * estimate. The header labels the figures as live or paper, mirroring the
 * Dashboard's dual-mode labelling — and in paper mode it states that the
 * figures still describe the real wallet, never the paper balance.
 *
 * @module components/PortfolioRiskPanel
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { portfolioService, RiskReport, RiskReportPosition } from "../services/portfolioService";
import { getApiErrorMessage } from "../utils/apiError";

/** The route also returns per-position rows, which the page's tables need. */
type RiskMetrics = RiskReport["metrics"] & { positions?: RiskReportPosition[] };

const usd = (value: number, digits = 2) =>
  `$${value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;

const pct = (value: number, digits = 1) => `${(value * 100).toFixed(digits)}%`;

const signedUsd = (value: number) => `${value >= 0 ? "+" : "-"}${usd(Math.abs(value))}`;

const shortTitle = (title: string, max = 26) =>
  title.length > max ? `${title.slice(0, max - 1)}…` : title;

function ratingTone(rating: string): "good" | "warn" | "bad" {
  const value = rating.toLowerCase();
  if (/low|minimal|conservative|safe|diversified/.test(value)) return "good";
  if (/high|elevated|critical|extreme|severe|concentrated/.test(value)) return "bad";
  return "warn";
}

const TONE_CLASS: Record<"good" | "warn" | "bad", string> = {
  good: "status-good",
  warn: "status-warn",
  bad: "status-bad",
};

const expiryLabel = (days: number) => (days <= 0 ? "today" : `${days}d`);

export default function PortfolioRiskPanel() {
  const [report, setReport] = useState<RiskReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchReport = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setReport(await portfolioService.getRiskReport());
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchReport();
  }, [fetchReport]);

  const metrics: RiskMetrics | null = report?.metrics ?? null;
  const narrative = report?.narrative ?? null;
  const positions = useMemo(
    () => (metrics?.positions ?? []).filter((position) => position.size > 0),
    [metrics],
  );
  const concentrationData = useMemo(
    () =>
      [...positions]
        .sort((a, b) => b.weight - a.weight)
        .slice(0, 8)
        .map((position) => ({
          name: shortTitle(position.market_title || "Position"),
          weight: position.weight,
          flagged: position.concentration_flag,
        })),
    [positions],
  );

  if (loading && !report) {
    return (
      <div className="p-6">
        <p className="text-soft text-sm">Loading risk report…</p>
      </div>
    );
  }

  if (error && !report) {
    return (
      <div className="p-6 space-y-3">
        <p className="text-soft text-sm">{error}</p>
        <button type="button" onClick={fetchReport} className="btn-muted text-sm">
          Retry
        </button>
      </div>
    );
  }

  if (!metrics) {
    return <p className="text-soft text-sm">Risk report unavailable.</p>;
  }

  const limits = metrics.risk_limits ?? {};
  const mode = String(limits.mode ?? (limits.simulation_mode ? "paper" : "live"));
  const isPaper = mode === "paper";
  const rating = narrative?.overall_risk_rating?.trim() || "";
  const ratingToneKey = rating ? ratingTone(rating) : "warn";

  return (
    <div className="space-y-6">
      {/* Header — always states whether the figures are live or paper */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-xl font-bold text-white">Portfolio Risk Report</h2>
            <span
              className={`chip ${isPaper ? "chip-warning" : "chip-success"}`}
              title={
                isPaper
                  ? "Paper mode is on, but these figures describe your real wallet exposure — not the paper balance below."
                  : "Live mode: figures describe real wallet exposure."
              }
            >
              {isPaper ? "PAPER MODE · REAL WALLET" : "LIVE"}
            </span>
          </div>
          <p className="text-soft text-sm mt-1">
            Concentration, loss bound, correlated clusters and upcoming catalysts — computed from
            your positions. The loss bound is a worst-case bound, the VaR is an estimate.
          </p>
          {isPaper && (
            <p className="text-soft text-xs mt-1">
              Paper mode is on: simulated fills run against the paper balance (
              {usd(Number(limits.paper_balance ?? 0))}), but every figure on this page describes
              your real wallet exposure.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {report?.generated_at && (
            <span className="text-xs text-soft">
              Generated {new Date(report.generated_at).toLocaleTimeString()}
            </span>
          )}
          <button
            type="button"
            onClick={fetchReport}
            disabled={loading}
            className="btn-muted text-sm"
          >
            {loading ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && <p className="text-soft text-sm">{error}</p>}

      {metrics.positions_count === 0 ? (
        <div className="surface-panel p-5">
          <p className="text-soft text-sm">
            No open positions — there is nothing to assess yet. Open a trade and the concentration,
            loss-bound and catalyst numbers appear here.
          </p>
        </div>
      ) : (
        <>
          {/* Stat cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <div className="surface-panel p-4">
              <p className="text-xs text-soft">Portfolio value</p>
              <p className="text-2xl font-bold text-white mt-1">{usd(metrics.portfolio_value)}</p>
              <p className="text-xs text-soft mt-1 truncate" title="USDC balance + position value">
                {usd(metrics.usdc_balance)} USDC +{" "}
                {usd(metrics.portfolio_value - metrics.usdc_balance)} in positions
              </p>
            </div>
            <div className="surface-panel p-4">
              <p className="text-xs text-soft">Loss bound</p>
              <p className="text-2xl font-bold text-white mt-1">{usd(metrics.loss_bound)}</p>
              <p className="text-xs text-soft mt-1 truncate" title="Total cost basis at risk">
                {pct(
                  metrics.portfolio_value > 0 ? metrics.loss_bound / metrics.portfolio_value : 0,
                )}{" "}
                of portfolio
              </p>
            </div>
            <div className="surface-panel p-4">
              <p className="text-xs text-soft">Max position weight</p>
              <p className="text-2xl font-bold text-white mt-1">
                {pct(metrics.max_position_weight)}
              </p>
              <p className="text-xs text-soft mt-1 truncate" title="Positions above 20%">
                {metrics.concentration_flags} over 20%
              </p>
            </div>
            <div className="surface-panel p-4">
              <p className="text-xs text-soft">Unrealized PnL</p>
              <p
                className={`text-2xl font-bold mt-1 ${
                  metrics.unrealized_pnl >= 0 ? "status-good" : "status-bad"
                }`}
              >
                {signedUsd(metrics.unrealized_pnl)}
              </p>
              <p className="text-xs text-soft mt-1 truncate" title="Marked to current prices">
                {metrics.positions_count} open position{metrics.positions_count === 1 ? "" : "s"}
              </p>
            </div>
          </div>

          {/* AI narrative */}
          <div className="surface-panel p-5">
            <div className="flex flex-wrap items-center gap-2 mb-3">
              <h3 className="text-base font-semibold text-white">Risk assessment</h3>
              {narrative && rating && (
                <span className={`chip ${TONE_CLASS[ratingToneKey]}`}>{rating}</span>
              )}
            </div>
            {narrative ? (
              <div className="grid md:grid-cols-2 gap-4">
                <div className="space-y-3">
                  <div>
                    <p className="text-xs text-soft mb-1">VaR estimate (95%)</p>
                    <p className="text-sm text-white">
                      {narrative.var_estimate || "Not estimated for this portfolio."}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-soft mb-1">Concentration flags</p>
                    <NarrativeList items={narrative.concentration_flags} />
                  </div>
                  <div>
                    <p className="text-xs text-soft mb-1">Correlated clusters</p>
                    <NarrativeList items={narrative.correlated_clusters} />
                  </div>
                </div>
                <div className="space-y-3">
                  <div>
                    <p className="text-xs text-soft mb-1">Tail scenarios</p>
                    <NarrativeList items={narrative.tail_scenarios} />
                  </div>
                  <div>
                    <p className="text-xs text-soft mb-1">Suggested hedges</p>
                    <NarrativeList items={narrative.hedges} />
                  </div>
                </div>
              </div>
            ) : (
              <p className="text-soft text-sm">
                AI narrative unavailable — the metrics above are still computed from your wallet.
              </p>
            )}
          </div>

          {/* Concentration chart + table */}
          <div className="surface-panel p-5">
            <h3 className="text-base font-semibold text-white mb-1">Concentration</h3>
            <p className="text-soft text-xs mb-3">
              Share of portfolio value per position; anything above 20% is flagged.
            </p>
            <div className="h-64 mb-4">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={concentrationData}
                  layout="vertical"
                  margin={{ top: 5, right: 20, bottom: 5, left: 20 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                  <XAxis
                    type="number"
                    domain={[0, 1]}
                    tickFormatter={(value: number) => `${Math.round(value * 100)}%`}
                    tick={{ fill: "#94a3b8", fontSize: 11 }}
                  />
                  <YAxis
                    type="category"
                    dataKey="name"
                    tick={{ fill: "#94a3b8", fontSize: 11 }}
                    width={150}
                  />
                  <Tooltip
                    formatter={(value: unknown) => [
                      typeof value === "number" ? pct(value) : "—",
                      "Weight",
                    ]}
                    contentStyle={{
                      backgroundColor: "#1e293b",
                      border: "1px solid #334155",
                      borderRadius: 8,
                    }}
                  />
                  <Bar dataKey="weight" radius={[0, 4, 4, 0]}>
                    {concentrationData.map((entry) => (
                      <Cell key={entry.name} fill={entry.flagged ? "#ef4444" : "#38bdf8"} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-soft text-xs text-left">
                    <th className="py-2 pr-4">Market</th>
                    <th className="py-2 pr-4">Outcome</th>
                    <th className="py-2 pr-4">Size</th>
                    <th className="py-2 pr-4">Value</th>
                    <th className="py-2 pr-4">Weight</th>
                    <th className="py-2">Cost basis</th>
                  </tr>
                </thead>
                <tbody>
                  {positions.map((position) => (
                    <tr
                      key={`${position.condition_id}-${position.outcome}`}
                      className={`border-t border-[var(--line)] ${
                        position.concentration_flag ? "bg-[#ef44441a]" : ""
                      }`}
                    >
                      <td className="py-2 pr-4 text-white">
                        {position.market_title}
                        {position.concentration_flag && (
                          <span className="status-bad text-xs ml-2">over 20%</span>
                        )}
                      </td>
                      <td className="py-2 pr-4 text-soft">{position.outcome || "—"}</td>
                      <td className="py-2 pr-4 text-soft">{position.size.toFixed(2)}</td>
                      <td className="py-2 pr-4 text-white">{usd(position.value)}</td>
                      <td className="py-2 pr-4 text-white">{pct(position.weight)}</td>
                      <td className="py-2 text-soft">{usd(position.cost_basis)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Clusters + catalysts */}
          <div className="grid md:grid-cols-2 gap-4">
            <div className="surface-panel p-5">
              <h3 className="text-base font-semibold text-white mb-1">Correlated clusters</h3>
              <p className="text-soft text-xs mb-3">
                Grouped deterministically by condition id and shared title tokens — a heuristic, not
                a category feed.
              </p>
              {metrics.clusters.length > 0 ? (
                <ul className="space-y-3">
                  {metrics.clusters.map((cluster) => (
                    <li key={cluster.label} className="surface-soft p-3">
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-sm text-white font-semibold">{cluster.label}</p>
                        <span className="text-xs text-soft">{pct(cluster.weight)}</span>
                      </div>
                      <ul className="mt-2 space-y-1">
                        {cluster.members.map((member) => (
                          <li key={member} className="text-xs text-soft">
                            {member}
                          </li>
                        ))}
                      </ul>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-soft text-sm">No overlapping exposures detected.</p>
              )}
            </div>

            <div className="surface-panel p-5">
              <h3 className="text-base font-semibold text-white mb-1">Upcoming catalysts</h3>
              <p className="text-soft text-xs mb-3">
                Market resolution dates inside the next 30 days.
              </p>
              {metrics.catalysts.length > 0 ? (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="text-soft text-xs text-left">
                        <th className="py-2 pr-4">Date</th>
                        <th className="py-2 pr-4">Market</th>
                        <th className="py-2">Resolves in</th>
                      </tr>
                    </thead>
                    <tbody>
                      {metrics.catalysts.map((catalyst) => (
                        <tr
                          key={`${catalyst.market_title}-${catalyst.end_date}`}
                          className="border-t border-[var(--line)]"
                        >
                          <td className="py-2 pr-4 text-soft">{catalyst.end_date}</td>
                          <td className="py-2 pr-4 text-white">{catalyst.market_title}</td>
                          <td
                            className={`py-2 ${
                              catalyst.days_to_expiry <= 3 ? "status-warn" : "text-soft"
                            }`}
                          >
                            {expiryLabel(catalyst.days_to_expiry)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="text-soft text-sm">No resolution dates inside 30 days.</p>
              )}
            </div>
          </div>

          {/* Live risk limits */}
          <div className="surface-panel p-5">
            <h3 className="text-base font-semibold text-white mb-3">Your live risk limits</h3>
            {Object.keys(limits).length > 0 ? (
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {Object.entries(limits).map(([key, value]) => (
                  <div key={key} className="surface-soft p-3">
                    <p className="text-xs text-soft">{key.replace(/_/g, " ")}</p>
                    <p className="text-sm font-semibold text-white mt-1 break-words">
                      {String(value)}
                    </p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-soft text-sm">No stored risk limits yet.</p>
            )}
            {limits.trading_halted && (
              <p className="status-bad text-sm mt-3">
                Trading is halted{limits.halt_reason ? `: ${String(limits.halt_reason)}` : "."}
              </p>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function NarrativeList({ items }: { items: string[] }) {
  if (items.length === 0) {
    return <p className="text-soft text-sm">—</p>;
  }
  return (
    <ul className="space-y-1">
      {items.map((item, index) => (
        <li key={`${index}-${item}`} className="text-sm text-white">
          • {item}
        </li>
      ))}
    </ul>
  );
}
