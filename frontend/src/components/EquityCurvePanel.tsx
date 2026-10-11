/**
 * Equity curve panel: hourly portfolio bankroll snapshots over time.
 *
 * Plots the bankroll series written by the hourly snapshot writer (plan 05)
 * as a line chart with a break-even reference line at the period's opening
 * bankroll, plus the period's change and worst peak-to-trough drawdown.
 *
 * Data fetching is a manual `useEffect` + `useCallback` pair — the repo has no
 * `QueryClientProvider` and zero `useQuery` usage despite `react-query` being
 * installed, so TanStack Query is deliberately not introduced here.
 *
 * Exactly one account is ever plotted: the fetch sends `is_paper` for the
 * selected mode, so the paper and live series are never drawn (or summarised)
 * against each other. The mode starts from `settings.simulation_mode` — the
 * same state source as AppLayout's Paper/Live toggle — and the panel's own
 * toggle overrides it and re-fetches.
 *
 * @module components/EquityCurvePanel
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { portfolioService, EquitySnapshotPoint, EquitySummary } from "../services/portfolioService";
import { settingsService } from "../services/settingsService";
import { getApiErrorMessage } from "../utils/apiError";

/** Period selector options; "all" is the whole recorded history. */
const PERIODS = ["24h", "7d", "30d", "all"] as const;
type Period = (typeof PERIODS)[number];

const PERIOD_LABELS: Record<Period, string> = {
  "24h": "24h",
  "7d": "7d",
  "30d": "30d",
  all: "All",
};

/** The two bankroll accounts: the real wallet, or the simulated paper one. */
type Mode = "live" | "paper";

/**
 * Fetch window per period. The API's default window is 7 days, so the wider
 * selectors must ask for one explicitly — each limit covers a full period of
 * hourly points with headroom.
 */
const PERIOD_WINDOWS: Record<Period, { hours: number; limit: number } | null> = {
  "24h": { hours: 24, limit: 48 },
  "7d": { hours: 24 * 7, limit: 200 },
  "30d": { hours: 24 * 30, limit: 800 },
  all: null,
};

/**
 * "All" has no bounded window, so it must state one: an epoch lower bound is
 * the explicit way to say "no lower bound". Without it the API silently
 * applies its 7-day default and the chart would disagree with the summary.
 */
const ALL_PERIOD_FROM = new Date(0).toISOString();
const ALL_PERIOD_LIMIT = 2000;

const usd = (value: number, digits = 2) =>
  `$${value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;

const signedUsd = (value: number) => `${value >= 0 ? "+" : "-"}${usd(Math.abs(value))}`;

const signedPct = (value: number) => `${value >= 0 ? "+" : "-"}${Math.abs(value).toFixed(2)}%`;

const hourLabel = (capturedAt: string) =>
  new Date(capturedAt).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

interface EquityPoint extends EquitySnapshotPoint {
  /** Chart x-axis key — the capture timestamp, pre-formatted. */
  label: string;
}

export default function EquityCurvePanel() {
  const [period, setPeriod] = useState<Period>("7d");
  const [points, setPoints] = useState<EquityPoint[]>([]);
  const [summary, setSummary] = useState<EquitySummary | null>(null);
  /** Account read from ``settings.simulation_mode`` (null until it loads). */
  const [settingsMode, setSettingsMode] = useState<Mode | null>(null);
  /** User's toggle; ``null`` means "follow the settings". */
  const [modeOverride, setModeOverride] = useState<Mode | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const mode: Mode = modeOverride ?? settingsMode ?? "live";

  const fetchEquity = useCallback(async () => {
    setLoading(true);
    setError(null);
    const fetchWindow = PERIOD_WINDOWS[period];
    // Settings are read first: they pick the account when the user has not
    // toggled anything, so the summary matches the series on screen. A
    // settings failure is not fatal — the live series is the safe default.
    let accountMode: Mode = modeOverride ?? "live";
    try {
      const settings = await settingsService.getUserSettings();
      accountMode = modeOverride ?? (settings.simulation_mode ? "paper" : "live");
    } catch {
      accountMode = modeOverride ?? "live";
    }
    const isPaper = accountMode === "paper";
    try {
      const [snapshots, summaryResp] = await Promise.all([
        portfolioService.getEquitySnapshots({
          from: fetchWindow
            ? new Date(Date.now() - fetchWindow.hours * 3_600_000).toISOString()
            : ALL_PERIOD_FROM,
          limit: fetchWindow ? fetchWindow.limit : ALL_PERIOD_LIMIT,
          isPaper,
        }),
        portfolioService.getEquitySummary(period, isPaper),
      ]);
      setSettingsMode(accountMode);
      // Oldest first: the writer appends, but ordering on the client keeps the
      // chart correct even if the API ever returns an unordered page.
      const sorted = [...snapshots].sort(
        (a, b) => new Date(a.captured_at).getTime() - new Date(b.captured_at).getTime(),
      );
      setPoints(sorted.map((point) => ({ ...point, label: hourLabel(point.captured_at) })));
      setSummary(summaryResp);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [period, modeOverride]);

  useEffect(() => {
    void fetchEquity();
  }, [fetchEquity]);

  /** One account per series, decided before the request is built. */
  const series = points.filter((point) =>
    mode === "paper" ? Boolean(point.is_paper) : !point.is_paper,
  );

  const chartData = useMemo(
    () => series.map((point) => ({ label: point.label, bankroll: point.bankroll })),
    [series],
  );

  /**
   * Break-even reference: the bankroll the period opened at. A zero (or
   * missing) open is meaningless as a line, so it is only drawn when real.
   */
  const breakEven = useMemo(() => {
    const open = series.length > 0 ? series[0].bankroll : (summary?.start ?? 0);
    return open > 0 ? open : null;
  }, [series, summary]);

  const latest = series.length > 0 ? series[series.length - 1].bankroll : 0;

  const toggleMode = () => setModeOverride(mode === "paper" ? "live" : "paper");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-white">Equity Curve</h2>
          <p className="text-soft text-sm mt-1">
            Hourly portfolio equity — USDC balance plus unrealized PnL
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={toggleMode}
            className={`px-3 py-1.5 rounded-lg text-sm ${
              mode === "paper" ? "btn-primary" : "bg-surface border border-border text-soft"
            }`}
            title={
              mode === "paper"
                ? "Showing the paper series (paper balance, not real equity)"
                : "Showing the live series (real wallet equity)"
            }
          >
            {mode === "paper" ? "Paper" : "Live"}
          </button>
          {PERIODS.map((option) => (
            <button
              key={option}
              onClick={() => setPeriod(option)}
              className={`px-3 py-1.5 rounded-lg text-sm ${
                period === option ? "btn-primary" : "bg-surface border border-border text-soft"
              }`}
            >
              {PERIOD_LABELS[option]}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-400 text-sm">
          {error}
        </div>
      )}

      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          title="Bankroll"
          value={usd(latest)}
          subtitle={
            mode === "paper"
              ? "Paper account — this is the paper balance, not real equity"
              : "USDC balance + unrealized PnL"
          }
        />
        <StatCard
          title="Period change"
          value={summary ? signedUsd(summary.delta) : "—"}
          subtitle={summary ? `${signedPct(summary.delta_pct)} over the period` : "—"}
        />
        <StatCard
          title="Max drawdown"
          value={summary ? `${summary.max_drawdown_pct.toFixed(2)}%` : "—"}
          subtitle="Worst peak-to-trough fall"
        />
        <StatCard
          title="Snapshots"
          value={summary ? String(summary.points) : "0"}
          subtitle={`${series.length} plotted in ${PERIOD_LABELS[period]}`}
        />
      </div>

      {/* Equity curve */}
      <div className="surface-panel p-5">
        <h3 className="text-base font-semibold text-white mb-1">Bankroll over time</h3>
        <p className="text-soft text-xs mb-3">
          One point per hour; the dashed line marks where the period opened.
        </p>
        {chartData.length > 1 ? (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chartData} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 11 }} />
                <YAxis
                  tick={{ fill: "#94a3b8", fontSize: 11 }}
                  tickFormatter={(value: unknown) =>
                    typeof value === "number" ? `$${value.toFixed(0)}` : ""
                  }
                />
                <Tooltip
                  formatter={(value: unknown) => [
                    typeof value === "number" ? usd(value) : "—",
                    "Bankroll",
                  ]}
                  contentStyle={{
                    backgroundColor: "#1e293b",
                    border: "1px solid #334155",
                    borderRadius: 8,
                  }}
                />
                {breakEven !== null && (
                  <ReferenceLine
                    y={breakEven}
                    stroke="#64748b"
                    strokeDasharray="3 3"
                    label={{ value: "break-even", fill: "#64748b", fontSize: 10 }}
                  />
                )}
                <Line
                  type="monotone"
                  dataKey="bankroll"
                  stroke="#38bdf8"
                  strokeWidth={2}
                  dot={false}
                  name="Bankroll"
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-soft text-sm">
            {loading
              ? "Loading equity history…"
              : `No equity history for the ${mode === "paper" ? "paper" : "live"} account yet — the hourly writer records your first point within an hour.`}
          </p>
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
