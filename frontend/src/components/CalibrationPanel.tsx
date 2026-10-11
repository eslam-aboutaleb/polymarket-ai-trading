/**
 * Forecast calibration dashboard: is the AI's probability actually right?
 *
 * Fetches the two `/api/analysis/calibration` endpoints and renders the
 * Brier score versus the 0.25 coin-flip baseline, the reliability curve
 * (observed YES frequency per stated-probability bucket, against the
 * perfect-calibration diagonal), mean Brier per model, and the recent
 * forecast rows with probability, market price, edge and resolution.
 *
 * Verdicts accumulate from deployment onward: legacy assessments carry
 * no probability and are surfaced in the data-quality strip rather than
 * the charts.
 *
 * @module components/CalibrationPanel
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useAuthStore } from "../store/authStore";
import { getApiErrorMessage } from "../utils/apiError";
import {
  CalibrationForecast,
  CalibrationGroupStats,
  CalibrationSummaryResponse,
  calibrationService,
} from "../services/calibrationService";

const WINDOW_OPTIONS = [7, 30, 90];

const BASELINE_BRIER = 0.25;

const brier = (value: number | null | undefined) => (value == null ? "—" : value.toFixed(3));

const pct = (value: number | null | undefined, digits = 1) =>
  value == null ? "—" : `${(value * 100).toFixed(digits)}%`;

const signed = (value: number | null | undefined) =>
  value == null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}`;

const modelColor = (meanBrier: number | null) =>
  meanBrier != null && meanBrier < BASELINE_BRIER ? "#22c55e" : "#f97316";

const shortMarket = (conditionId: string | null) =>
  conditionId ? `${conditionId.slice(0, 10)}…${conditionId.slice(-4)}` : "—";

const modelLabel = (row: CalibrationGroupStats) => row.model_name || "unknown";

interface ReliabilityPoint {
  bucket: string;
  predicted: number;
  actual: number;
  count: number;
}

export default function CalibrationPanel() {
  const { isAdmin } = useAuthStore();
  const [windowDays, setWindowDays] = useState(30);
  const [globalScope, setGlobalScope] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [summary, setSummary] = useState<CalibrationSummaryResponse | null>(null);
  const [forecasts, setForecasts] = useState<CalibrationForecast[]>([]);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const scope: "user" | "global" = globalScope ? "global" : "user";
      const query = { window_days: windowDays, scope };
      const [summaryResp, forecastsResp] = await Promise.all([
        calibrationService.getSummary(query),
        calibrationService.getForecasts(50, query),
      ]);
      setSummary(summaryResp);
      setForecasts(forecastsResp.forecasts);
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [windowDays, globalScope]);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Reliability points come back as deciles with the mean stated
  // probability; only resolved buckets carry both coordinates.
  const reliabilityData = useMemo<ReliabilityPoint[]>(
    () =>
      (summary?.reliability ?? [])
        .filter(
          (b): b is ReliabilityPoint => b.predicted != null && b.actual != null && b.count > 0,
        )
        .map((b) => ({
          bucket: b.bucket,
          predicted: b.predicted,
          actual: b.actual,
          count: b.count,
        })),
    [summary],
  );

  const modelRows = useMemo(() => summary?.by_model ?? [], [summary]);
  const dataQuality = summary?.data_quality ?? null;
  const brierSkill = summary?.mean_brier == null ? null : 1 - summary.mean_brier / BASELINE_BRIER;

  if (loading && !summary) {
    return (
      <div className="p-6">
        <p className="text-soft text-sm">Loading calibration...</p>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold text-white">Forecast Calibration</h2>
          <p className="text-soft text-sm mt-1">
            Brier score and reliability per model and task (lower Brier is better)
          </p>
        </div>
        <div className="flex items-center gap-2">
          {isAdmin && (
            <button
              onClick={() => setGlobalScope((prev) => !prev)}
              className={`px-3 py-1.5 rounded-lg text-sm ${
                globalScope ? "btn-primary" : "bg-surface border border-border text-soft"
              }`}
            >
              {globalScope ? "Global scope" : "My copy trades"}
            </button>
          )}
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
      {dataQuality && (dataQuality.missing_model > 0 || dataQuality.legacy_excluded > 0) && (
        <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-3 text-yellow-400 text-sm">
          Data quality: {dataQuality.legacy_excluded} pre-calibration verdicts excluded (no
          probability recorded, never backfilled), {dataQuality.missing_model} verdicts without
          model attribution (cli-agent never echoes a model).
        </div>
      )}

      {/* Headline stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          title="Forecasts"
          value={`${summary?.sample_count ?? 0}`}
          subtitle={`${summary?.resolved_count ?? 0} resolved`}
        />
        <StatCard
          title="Mean Brier"
          value={brier(summary?.mean_brier)}
          subtitle={`coin-flip baseline ${BASELINE_BRIER.toFixed(2)}`}
        />
        <StatCard
          title="Brier skill"
          value={brierSkill == null ? "—" : pct(brierSkill)}
          subtitle="vs the 0.25 coin-flip baseline"
        />
        <StatCard
          title="Avg edge"
          value={signed(avgEdge(forecasts))}
          subtitle="AI probability minus market price"
        />
      </div>

      {/* Reliability curve */}
      <div className="surface-panel p-5">
        <h3 className="text-base font-semibold text-white mb-1">Reliability curve</h3>
        <p className="text-soft text-xs mb-3">
          Observed YES frequency per stated probability bucket — a calibrated forecaster tracks the
          dashed diagonal
        </p>
        {reliabilityData.length > 0 ? (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={reliabilityData} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis
                  type="number"
                  dataKey="predicted"
                  domain={[0, 1]}
                  tick={{ fill: "#94a3b8", fontSize: 11 }}
                  tickFormatter={(value: unknown) =>
                    typeof value === "number" ? value.toFixed(1) : ""
                  }
                />
                <YAxis
                  type="number"
                  domain={[0, 1]}
                  tick={{ fill: "#94a3b8", fontSize: 11 }}
                  tickFormatter={(value: unknown) =>
                    typeof value === "number" ? value.toFixed(1) : ""
                  }
                />
                <Tooltip
                  formatter={(value: unknown, name: unknown) => [
                    typeof value === "number" ? pct(value) : "—",
                    name === "actual" ? "Observed YES rate" : "Stated probability",
                  ]}
                  labelFormatter={() => ""}
                  contentStyle={{
                    backgroundColor: "#1e293b",
                    border: "1px solid #334155",
                    borderRadius: 8,
                  }}
                />
                <ReferenceLine
                  segment={[
                    { x: 0, y: 0 },
                    { x: 1, y: 1 },
                  ]}
                  stroke="#64748b"
                  strokeDasharray="3 3"
                  label={{ value: "perfect calibration", fill: "#64748b", fontSize: 10 }}
                />
                <Line
                  type="linear"
                  dataKey="actual"
                  stroke="#38bdf8"
                  strokeWidth={2}
                  dot={{ r: 4 }}
                  name="actual"
                />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-soft text-sm">
            No resolved forecasts in this window yet — reliability needs markets that have settled.
          </p>
        )}
      </div>

      {/* Mean Brier per model */}
      <div className="surface-panel p-5">
        <h3 className="text-base font-semibold text-white mb-1">Mean Brier by model</h3>
        <p className="text-soft text-xs mb-3">
          Green beats the 0.25 coin-flip baseline; verdicts without model attribution are counted in
          data quality instead
        </p>
        {modelRows.length > 0 ? (
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={modelRows} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                <XAxis
                  dataKey="model_name"
                  tickFormatter={(value: unknown) => String(value || "unknown")}
                  tick={{ fill: "#94a3b8", fontSize: 11 }}
                />
                <YAxis
                  allowDecimals={false}
                  tick={{ fill: "#94a3b8", fontSize: 11 }}
                  tickFormatter={(value: unknown) =>
                    typeof value === "number" ? value.toFixed(2) : ""
                  }
                />
                <Tooltip
                  formatter={(value: unknown, _name: unknown, entry: unknown) => [
                    typeof value === "number" ? value.toFixed(3) : "—",
                    "Mean Brier",
                  ]}
                  labelFormatter={(label: unknown) => String(label || "unknown")}
                  contentStyle={{
                    backgroundColor: "#1e293b",
                    border: "1px solid #334155",
                    borderRadius: 8,
                  }}
                />
                <ReferenceLine
                  y={BASELINE_BRIER}
                  stroke="#64748b"
                  strokeDasharray="3 3"
                  label={{ value: "coin flip", fill: "#64748b", fontSize: 10 }}
                />
                <Bar dataKey="mean_brier" radius={[4, 4, 0, 0]}>
                  {modelRows.map((row) => (
                    <Cell key={row.model_name || "unknown"} fill={modelColor(row.mean_brier)} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="text-soft text-sm">
            No verdicts with model attribution in this window yet.
          </p>
        )}
      </div>

      {/* Recent forecasts table */}
      <div className="surface-panel p-5">
        <h3 className="text-base font-semibold text-white mb-3">Recent forecasts</h3>
        {forecasts.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-soft border-b border-border">
                  <th className="pb-2 pr-4">Market</th>
                  <th className="pb-2 pr-4">Task</th>
                  <th className="pb-2 pr-4">Model</th>
                  <th className="pb-2 pr-4">AI prob</th>
                  <th className="pb-2 pr-4">Price</th>
                  <th className="pb-2 pr-4">Edge</th>
                  <th className="pb-2 pr-4">Resolved</th>
                  <th className="pb-2">Brier</th>
                </tr>
              </thead>
              <tbody>
                {forecasts.map((forecast) => (
                  <tr
                    key={`${forecast.condition_id}-${forecast.created_at}`}
                    className="border-b border-border/50 text-white"
                  >
                    <td className="py-2 pr-4 max-w-[180px] truncate mono">
                      {shortMarket(forecast.condition_id)}
                    </td>
                    <td className="py-2 pr-4">{forecast.task || "—"}</td>
                    <td className="py-2 pr-4">{forecast.model || "unknown"}</td>
                    <td className="py-2 pr-4">{pct(forecast.ai_probability)}</td>
                    <td className="py-2 pr-4">{pct(forecast.market_price)}</td>
                    <td
                      className={`py-2 pr-4 ${
                        forecast.edge == null
                          ? "text-soft"
                          : forecast.edge > 0
                            ? "text-green-400"
                            : "text-red-400"
                      }`}
                    >
                      {signed(forecast.edge)}
                    </td>
                    <td className="py-2 pr-4">{forecast.resolved_outcome || "—"}</td>
                    <td className="py-2">{brier(forecast.brier_score)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-soft text-sm">No forecasts recorded in this window yet.</p>
        )}
      </div>
    </div>
  );
}

function avgEdge(forecasts: CalibrationForecast[]): number | null {
  const edges = forecasts
    .map((forecast) => forecast.edge)
    .filter((edge): edge is number => edge != null);
  if (edges.length === 0) return null;
  return Number((edges.reduce((sum, edge) => sum + edge, 0) / edges.length).toFixed(4));
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
