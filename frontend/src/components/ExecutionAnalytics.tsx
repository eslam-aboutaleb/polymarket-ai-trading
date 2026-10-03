/**
 * Execution analytics dashboard: execution quality per strategy.
 *
 * Fetches the three `/api/trades/analytics` endpoints and renders
 * fill-rate stat cards, the slippage distribution (BarChart),
 * cumulative fee drag vs gross realized PnL (LineChart) and the
 * Edge Score per strategy (bar gauge, 0–100, 50 = break-even).
 *
 * Metrics accumulate from deployment onward: pre-migration trades
 * lack expected-price data and are surfaced in the data-quality
 * strip rather than the charts.
 *
 * @module components/ExecutionAnalytics
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { getApiErrorMessage } from "../utils/apiError";
import {
  EdgeScoreRow,
  ExecutionStrategyStats,
  ExecutionTradeRecord,
  executionAnalyticsService,
  ExecutionSummaryResponse,
  EdgeScoreResponse,
} from "../services/executionAnalyticsService";

const WINDOW_OPTIONS = [7, 30, 90];

const STRATEGY_LABELS: Record<string, string> = {
  copy: "Copy",
  manual: "Manual",
  market_maker: "Market maker",
  inverse: "Inverse",
  stop_loss: "Stop-loss",
  take_profit: "Take-profit",
  latency_arb: "Latency arb",
};

const SLIPPAGE_BUCKETS = [
  { label: "< -100", min: -Infinity, max: -100 },
  { label: "-100…0", min: -100, max: 0 },
  { label: "0…100", min: 0, max: 100 },
  { label: "100…500", min: 100, max: 500 },
  { label: "> 500", min: 500, max: Infinity },
];

const strategyLabel = (strategy: string) => STRATEGY_LABELS[strategy] || strategy;

const pct = (value: number | null | undefined, digits = 1) =>
  value == null ? "—" : `${(value * 100).toFixed(digits)}%`;

const usd = (value: number | null | undefined, digits = 2) =>
  value == null ? "—" : `$${value.toFixed(digits)}`;

const bps = (value: number | null | undefined) => (value == null ? "—" : `${value.toFixed(1)} bps`);

interface SlippageBucket {
  bucket: string;
  trades: number;
}

interface CumulativePoint {
  time: string;
  cumulative_fees: number;
  cumulative_pnl: number;
}

function bucketSlippage(trades: ExecutionTradeRecord[]): SlippageBucket[] {
  const counts = new Array(SLIPPAGE_BUCKETS.length).fill(0);
  for (const trade of trades) {
    const slippage = trade.slippage_bps;
    if (slippage == null) continue;
    const idx = SLIPPAGE_BUCKETS.findIndex((b) => slippage >= b.min && slippage < b.max);
    if (idx >= 0) counts[idx] += 1;
  }
  return SLIPPAGE_BUCKETS.map((b, i) => ({ bucket: b.label, trades: counts[i] }));
}

function cumulativeSeries(trades: ExecutionTradeRecord[]): CumulativePoint[] {
  const filled = trades
    .filter((t) => t.executed_at != null)
    .sort(
      (a, b) =>
        new Date(a.executed_at as string).getTime() - new Date(b.executed_at as string).getTime(),
    );
  let fees = 0;
  let pnl = 0;
  return filled.map((t) => {
    fees += t.fee_paid ?? 0;
    pnl += t.pnl ?? 0;
    return {
      time: new Date(t.executed_at as string).toLocaleDateString(),
      cumulative_fees: Number(fees.toFixed(4)),
      cumulative_pnl: Number(pnl.toFixed(4)),
    };
  });
}

function edgeColor(score: number): string {
  return score >= 50 ? "#22c55e" : "#ef4444";
}

export default function ExecutionAnalytics() {
  const [windowDays, setWindowDays] = useState(30);
  const [strategyFilter, setStrategyFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [summary, setSummary] = useState<ExecutionSummaryResponse | null>(null);
  const [edgeScores, setEdgeScores] = useState<EdgeScoreResponse | null>(null);
  const [trades, setTrades] = useState<ExecutionTradeRecord[]>([]);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const query = { window_days: windowDays };
      const [summaryResp, edgeResp, tradesResp] = await Promise.all([
        executionAnalyticsService.getSummary(query),
        executionAnalyticsService.getEdgeScores(query),
        executionAnalyticsService.getTrades(strategyFilter || undefined, 200),
      ]);
      setSummary(summaryResp);
      setEdgeScores(edgeResp);
      setTrades(tradesResp.trades);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [windowDays, strategyFilter]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const slippageData = useMemo(() => bucketSlippage(trades), [trades]);
  const cumulativeData = useMemo(() => cumulativeSeries(trades), [trades]);
  const edgeRows = useMemo(() => (edgeScores?.strategies || []) as EdgeScoreRow[], [edgeScores]);
  const strategyStats = useMemo(() => {
    const rows: ExecutionStrategyStats[] = summary ? Object.values(summary.per_strategy) : [];
    return rows;
  }, [summary]);

  const totals = summary?.totals ?? null;
  const dataQuality = summary?.data_quality ?? null;

  if (loading && !summary) {
    return (
      <div className="p-6">
        <p className="text-soft text-sm">Loading execution analytics...</p>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-white">Execution Analytics</h2>
          <p className="text-soft text-sm mt-1">
            Slippage, fee drag, fill rate and Edge Score per strategy
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={strategyFilter}
            onChange={(e) => setStrategyFilter(e.target.value)}
            className="bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-white"
          >
            <option value="">All strategies</option>
            {edgeRows.map((row) => (
              <option key={row.strategy} value={row.strategy}>
                {strategyLabel(row.strategy)}
              </option>
            ))}
          </select>
          {WINDOW_OPTIONS.map((days) => (
            <button
              key={days}
              onClick={() => setWindowDays(days)}
              className={`px-3 py-1.5 rounded-lg text-sm ${
                windowDays === days ? "btn-primary" : "bg-surface border border-border text-soft"
              }`}
            >
              {days}d
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* Data quality strip */}
      {dataQuality &&
        (dataQuality.legacy_trades > 0 ||
          dataQuality.missing_expected_price > 0 ||
          dataQuality.missing_fee > 0) && (
          <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-3 text-yellow-400 text-sm">
            Data quality: {dataQuality.legacy_trades} pre-analytics trades excluded,{" "}
            {dataQuality.missing_expected_price} filled trades missing expected price,{" "}
            {dataQuality.missing_fee} fills missing fee data (treated as $0). Metrics accumulate
            from deployment onward.
          </div>
        )}

      {/* Fill-rate & headline stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          title="Fill rate"
          value={pct(totals?.fill_rate)}
          subtitle={`${totals?.filled ?? 0}/${totals?.submissions ?? 0} filled`}
        />
        <StatCard
          title="Avg slippage"
          value={bps(totals?.avg_slippage_bps)}
          subtitle={`median ${bps(totals?.median_slippage_bps)}`}
        />
        <StatCard
          title="Fee drag"
          value={
            totals?.fee_drag_ratio == null ? "—" : `${(totals.fee_drag_ratio * 100).toFixed(1)}%`
          }
          subtitle={`${usd(totals?.total_fees)} fees vs ${usd(totals?.gross_realized_pnl)} gross PnL`}
        />
        <StatCard
          title="Win rate"
          value={pct(totals?.win_rate)}
          subtitle={`${totals?.wins ?? 0}W / ${totals?.losses ?? 0}L`}
        />
      </div>

      {/* Per-strategy fill-rate cards */}
      {strategyStats.length > 0 && (
        <div className="surface-panel p-5">
          <h3 className="text-base font-semibold text-white mb-3">Fill rate by strategy</h3>
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
            {strategyStats.map((stats) => (
              <div key={stats.strategy} className="bg-surface-alt rounded-lg p-3">
                <p className="text-xs text-soft">{strategyLabel(stats.strategy)}</p>
                <p className="text-lg font-bold text-white">{pct(stats.fill_rate)}</p>
                <p className="text-xs text-soft">
                  {stats.filled}/{stats.submissions} filled · edge{" "}
                  {stats.edge_score == null ? "—" : stats.edge_score.toFixed(0)}
                </p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Edge Score per strategy */}
      <div className="surface-panel p-5">
        <h3 className="text-base font-semibold text-white mb-1">Edge Score by strategy</h3>
        <p className="text-soft text-xs mb-3">
          Expectancy per unit of average risk, normalized to 0–100 (50 = break-even)
        </p>
        {edgeRows.some((row) => row.edge_score != null) ? (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={edgeRows.filter((row) => row.edge_score != null)}
                layout="vertical"
                margin={{ top: 5, right: 20, bottom: 5, left: 80 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis type="number" domain={[0, 100]} tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <YAxis
                  type="category"
                  dataKey="strategy"
                  tickFormatter={strategyLabel}
                  tick={{ fill: "#94a3b8", fontSize: 11 }}
                  width={75}
                />
                <Tooltip
                  formatter={(value: unknown) => [
                    typeof value === "number" ? value.toFixed(1) : "—",
                    "Edge Score",
                  ]}
                  labelFormatter={(label: unknown) => strategyLabel(String(label))}
                  contentStyle={{
                    backgroundColor: "#1e293b",
                    border: "1px solid #334155",
                    borderRadius: 8,
                  }}
                />
                <ReferenceLine
                  x={50}
                  stroke="#64748b"
                  strokeDasharray="3 3"
                  label={{ value: "break-even", fill: "#64748b", fontSize: 10 }}
                />
                <Bar dataKey="edge_score" radius={[0, 4, 4, 0]}>
                  {edgeRows
                    .filter((row) => row.edge_score != null)
                    .map((row) => (
                      <Cell key={row.strategy} fill={edgeColor(row.edge_score as number)} />
                    ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-soft text-sm">
            No closed trades with PnL data yet — Edge Score needs wins and losses from PnL
            reconciliation.
          </p>
        )}
      </div>

      {/* Slippage distribution */}
      <div className="surface-panel p-5">
        <h3 className="text-base font-semibold text-white mb-1">Slippage distribution</h3>
        <p className="text-soft text-xs mb-3">
          Filled trades by signed slippage bucket (bps vs expected price)
        </p>
        {slippageData.some((b) => b.trades > 0) ? (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={slippageData} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis dataKey="bucket" tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <YAxis allowDecimals={false} tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "#1e293b",
                    border: "1px solid #334155",
                    borderRadius: 8,
                  }}
                />
                <Bar dataKey="trades" fill="#38bdf8" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-soft text-sm">No fills with expected-price data in this window yet.</p>
        )}
      </div>

      {/* Cumulative fee drag vs gross PnL */}
      <div className="surface-panel p-5">
        <h3 className="text-base font-semibold text-white mb-1">
          Cumulative fee drag vs gross realized PnL
        </h3>
        <p className="text-soft text-xs mb-3">
          Running totals over filled trades (fees vs reconciled PnL)
        </p>
        {cumulativeData.length > 0 ? (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={cumulativeData} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis dataKey="time" tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <YAxis
                  tick={{ fill: "#94a3b8", fontSize: 11 }}
                  tickFormatter={(value: unknown) =>
                    typeof value === "number" ? `$${value.toFixed(0)}` : ""
                  }
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: "#1e293b",
                    border: "1px solid #334155",
                    borderRadius: 8,
                  }}
                />
                <Legend />
                <Line
                  type="monotone"
                  dataKey="cumulative_pnl"
                  stroke="#22c55e"
                  strokeWidth={2}
                  dot={false}
                  name="Gross realized PnL"
                />
                <Line
                  type="monotone"
                  dataKey="cumulative_fees"
                  stroke="#f97316"
                  strokeWidth={2}
                  dot={false}
                  name="Cumulative fees"
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-soft text-sm">No filled trades in this window yet.</p>
        )}
      </div>

      {/* Per-trade detail table */}
      <div className="surface-panel p-5">
        <h3 className="text-base font-semibold text-white mb-3">Recent executions</h3>
        {trades.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-soft border-b border-border">
                  <th className="pb-2 pr-4">Market</th>
                  <th className="pb-2 pr-4">Strategy</th>
                  <th className="pb-2 pr-4">Side</th>
                  <th className="pb-2 pr-4">Expected</th>
                  <th className="pb-2 pr-4">Filled</th>
                  <th className="pb-2 pr-4">Slippage</th>
                  <th className="pb-2 pr-4">Fee</th>
                  <th className="pb-2 pr-4">Latency</th>
                  <th className="pb-2">Status</th>
                </tr>
              </thead>
              <tbody>
                {trades.map((trade) => (
                  <tr key={trade.id} className="border-b border-border/50 text-white">
                    <td className="py-2 pr-4 max-w-[180px] truncate">{trade.market_id || "—"}</td>
                    <td className="py-2 pr-4">
                      {trade.strategy_source ? strategyLabel(trade.strategy_source) : "—"}
                    </td>
                    <td className="py-2 pr-4 uppercase">{trade.action}</td>
                    <td className="py-2 pr-4">{trade.expected_price?.toFixed(4) ?? "—"}</td>
                    <td className="py-2 pr-4">{trade.filled_price?.toFixed(4) ?? "—"}</td>
                    <td
                      className={`py-2 pr-4 ${
                        trade.slippage_bps == null
                          ? "text-soft"
                          : trade.slippage_bps > 0
                            ? "text-red-400"
                            : "text-green-400"
                      }`}
                    >
                      {bps(trade.slippage_bps)}
                    </td>
                    <td className="py-2 pr-4">
                      {trade.fee_paid == null ? "—" : usd(trade.fee_paid, 4)}
                    </td>
                    <td className="py-2 pr-4">
                      {trade.latency_ms == null ? "—" : `${trade.latency_ms.toFixed(0)} ms`}
                    </td>
                    <td className="py-2">{trade.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-soft text-sm">No trades in this window.</p>
        )}
      </div>
    </div>
  );
}

function StatCard({ title, value, subtitle }: { title: string; value: string; subtitle: string }) {
  return (
    <div className="surface-panel p-4">
      <p className="text-xs text-soft">{title}</p>
      <p className="text-2xl font-bold text-white mt-1">{value}</p>
      <p className="text-xs text-soft mt-1 truncate" title={subtitle}>
        {subtitle}
      </p>
    </div>
  );
}
