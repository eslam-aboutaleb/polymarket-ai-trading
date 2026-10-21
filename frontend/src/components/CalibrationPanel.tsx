/**
 * Forecast calibration dashboard: is the AI's probability actually right?
 *
 * Fetches the `/api/analysis/calibration` endpoints and renders the
 * Brier score versus the 0.25 coin-flip baseline, the reliability curve
 * (observed YES frequency per stated-probability bucket, against the
 * perfect-calibration diagonal), mean Brier per model, and the recent
 * forecast rows with probability, market price, edge and resolution.
 *
 * Model governance (plan 04) extends the same rows: a drift section
 * (mean Brier per model/task over the window against the baseline, with
 * the backend's `drifted` flag), calibration grouped by the prompt
 * version that produced each verdict, verdict feedback on every row,
 * and — for admins only — the A/B experiment table plus arm comparison.
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
import { Banner, useToast } from "./ui";
import VerdictFeedbackControl from "./VerdictFeedbackControl";
import {
  CalibrationDriftRow,
  CalibrationForecast,
  CalibrationGroupStats,
  CalibrationSummaryResponse,
  ExperimentComparisonResponse,
  ExperimentConfig,
  ExperimentStatus,
  LLMExperiment,
  PromptVersionGroup,
  calibrationAdminService,
  calibrationService,
} from "../services/calibrationService";

const WINDOW_OPTIONS = [7, 30, 90];

const BASELINE_BRIER = 0.25;

// Group key `calibration.py` uses for rows written before prompt
// versioning existed (plan 04 never backfills prompt_version).
const UNVERSIONED_LABEL = "(unversioned)";

// `experiments.py` only counts a comparison below this as noise.
const MIN_RESOLVED_FOR_VERDICT = 20;

const EXPERIMENT_STATUS_LABELS: Record<string, string> = {
  active: "Active",
  paused: "Paused",
  done: "Retired",
};

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

const truncate = (value: string, max: number) =>
  value.length <= max ? value : `${value.slice(0, max)}…`;

const driftRowLabel = (row: CalibrationDriftRow) =>
  `${row.model_name || "unknown"} · ${row.task || "unknown"}`;

// The endpoint always sends the baseline, but a stale payload should
// not blank the table.
const baselineOf = (row: CalibrationDriftRow) => row.baseline_brier ?? BASELINE_BRIER;

const promptVersionLabel = (version: string) =>
  version === UNVERSIONED_LABEL ? UNVERSIONED_LABEL : truncate(version, 12);

interface ReliabilityPoint {
  bucket: string;
  predicted: number;
  actual: number;
  count: number;
}

interface DriftPoint {
  label: string;
  fullLabel: string;
  mean_brier: number | null;
  samples: number;
  drifted: boolean;
}

interface DriftDotProps {
  cx?: number;
  cy?: number;
  payload?: DriftPoint;
}

// Per-point dots so a drifted (model, task) reads as red next to the
// green ones, reusing the hexes the panel's per-model bars already use.
function renderDriftDot(props: DriftDotProps) {
  const { cx, cy, payload } = props;
  if (cx == null || cy == null || payload == null) return <g />;
  return (
    <circle
      cx={cx}
      cy={cy}
      r={payload.drifted ? 5.5 : 4}
      fill={payload.drifted ? "#ef4444" : "#22c55e"}
      stroke="#1e293b"
      strokeWidth={1}
    />
  );
}

export default function CalibrationPanel() {
  const { isAdmin } = useAuthStore();
  const [windowDays, setWindowDays] = useState(30);
  const [globalScope, setGlobalScope] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [summary, setSummary] = useState<CalibrationSummaryResponse | null>(null);
  const [forecasts, setForecasts] = useState<CalibrationForecast[]>([]);
  const [promptVersions, setPromptVersions] = useState<PromptVersionGroup[]>([]);
  const [drift, setDrift] = useState<CalibrationDriftRow[] | null>(null);
  const [driftMeta, setDriftMeta] = useState<{ min_samples: number; margin: number } | null>(null);
  const [governanceError, setGovernanceError] = useState<string | null>(null);

  const fetchData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const scope: "user" | "global" = globalScope ? "global" : "user";
      const query = { window_days: windowDays, scope };
      const [summaryResp, forecastsResp, promptResp] = await Promise.all([
        calibrationService.getSummary(query),
        calibrationService.getForecasts(50, query),
        calibrationService.getSummaryByPromptVersion(query),
      ]);
      setSummary(summaryResp);
      setForecasts(forecastsResp.forecasts);
      setPromptVersions(promptResp.versions);
      setGovernanceError(null);

      // Drift aggregates every model/task pair regardless of scope, and
      // the route only opens it to admins, so the section is admin-only
      // and always asks for the global report.
      if (isAdmin) {
        try {
          const driftResp = await calibrationService.getDrift({
            window_days: windowDays,
            scope: "global",
          });
          setDrift(driftResp.series);
          const first = driftResp.series[0];
          setDriftMeta({
            min_samples: driftResp.min_samples_for_drift,
            margin: first?.drift_margin ?? 0.02,
          });
        } catch (driftErr) {
          setDrift(null);
          setDriftMeta(null);
          setGovernanceError(getApiErrorMessage(driftErr, "Could not load drift metrics"));
        }
      } else {
        setDrift(null);
        setDriftMeta(null);
      }
    } catch (err) {
      setError(getApiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [windowDays, globalScope, isAdmin]);

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

  // Worst-first, matching the order the drift endpoint already returns.
  const driftPoints = useMemo<DriftPoint[]>(
    () =>
      (drift ?? []).map((row) => ({
        label: truncate(driftRowLabel(row), 16),
        fullLabel: driftRowLabel(row),
        mean_brier: row.mean_brier,
        samples: row.samples,
        drifted: row.drifted,
      })),
    [drift],
  );

  const driftedCount = useMemo(() => (drift ?? []).filter((row) => row.drifted).length, [drift]);

  const driftChartSummary = useMemo(() => {
    if (driftPoints.length === 0) return "";
    const points = driftPoints
      .map(
        (point) =>
          `${point.fullLabel} ${point.mean_brier == null ? "no score" : point.mean_brier.toFixed(3)}`,
      )
      .join("; ");
    return (
      `Mean Brier per model and task over the last ${windowDays} days, worst first: ${points}. ` +
      `Coin-flip baseline 0.25. ${driftedCount} of ${driftPoints.length} flagged as drifted.`
    );
  }, [driftPoints, driftedCount, windowDays]);

  // `/forecasts` does not serialise `assessments.id` yet, so the rating
  // column only appears once rows carry one.
  const canRateVerdicts = useMemo(() => forecasts.some((row) => row.id != null), [forecasts]);

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
      <div
        className="flex flex-wrap items-center justify-between gap-3"
        data-tutorial="calibration"
      >
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
              aria-pressed={globalScope}
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
              aria-pressed={windowDays === days}
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
        <Banner variant="error" onDismiss={() => setError(null)} onRetry={fetchData}>
          {error}
        </Banner>
      )}

      {/* Data quality strip */}
      {dataQuality &&
        (dataQuality.missing_model > 0 ||
          dataQuality.legacy_excluded > 0 ||
          dataQuality.invalid_legacy_resolution > 0) && (
          <div className="bg-yellow-500/10 border border-yellow-500/30 rounded-lg p-3 text-yellow-400 text-sm">
            Data quality: {dataQuality.legacy_excluded} pre-calibration verdicts excluded (no
            probability recorded, never backfilled), {dataQuality.missing_model} verdicts without
            model attribution (cli-agent never echoes a model),{" "}
            {dataQuality.invalid_legacy_resolution} verdicts with a non-binary resolution label
            (excluded from every aggregate).
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

      {/* Model drift (plan 04) */}
      {isAdmin && (
        <div className="surface-panel p-5">
          <h3 className="text-base font-semibold text-white mb-1">Model drift</h3>
          <p className="text-soft text-xs mb-3">
            Mean Brier per model/task over the last {windowDays} days, worst first, against the 0.25
            coin-flip baseline. A pair is flagged only with at least {driftMeta?.min_samples ?? 20}{" "}
            resolved verdicts and either more than {(driftMeta?.margin ?? 0.02).toFixed(2)} excess
            over the baseline or a rising slope.
          </p>
          {governanceError && (
            <Banner
              variant="error"
              className="mb-3"
              onDismiss={() => setGovernanceError(null)}
              onRetry={fetchData}
            >
              {governanceError}
            </Banner>
          )}
          {drift != null && drift.length > 0 ? (
            <>
              <p className="text-xs text-soft mb-2">
                {driftedCount} of {drift.length} model/task pair
                {drift.length === 1 ? "" : "s"} flagged as drifted
              </p>
              <div className="h-64" role="img" aria-label={driftChartSummary}>
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={driftPoints} margin={{ top: 5, right: 20, bottom: 5, left: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                    <XAxis
                      dataKey="label"
                      tick={{ fill: "#94a3b8", fontSize: 11 }}
                      interval={0}
                      tickFormatter={(value: unknown) => truncate(String(value ?? ""), 16)}
                    />
                    <YAxis
                      domain={[0, "auto"]}
                      tick={{ fill: "#94a3b8", fontSize: 11 }}
                      tickFormatter={(value: unknown) =>
                        typeof value === "number" ? value.toFixed(2) : ""
                      }
                    />
                    <Tooltip
                      formatter={(value: unknown) => [
                        typeof value === "number" ? value.toFixed(3) : "—",
                        "Mean Brier",
                      ]}
                      labelFormatter={(label: unknown) => String(label ?? "unknown")}
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
                    <Line
                      type="linear"
                      dataKey="mean_brier"
                      name="Mean Brier"
                      stroke="#38bdf8"
                      strokeWidth={2}
                      dot={renderDriftDot}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
              <div className="overflow-x-auto mt-4">
                <table className="table-theme w-full text-sm">
                  <thead>
                    <tr>
                      <th className="text-left py-2 pr-4">Model</th>
                      <th className="text-left py-2 pr-4">Task</th>
                      <th className="text-right py-2 pr-4">Mean Brier</th>
                      <th className="text-right py-2 pr-4">Baseline</th>
                      <th className="text-right py-2 pr-4">Excess</th>
                      <th className="text-right py-2 pr-4">Samples</th>
                      <th className="text-right py-2 pr-4">Slope/day</th>
                      <th className="text-left py-2">Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(drift ?? []).map((row) => (
                      <tr key={`${row.model_name ?? "unknown"}-${row.task ?? "unknown"}`}>
                        <td className="py-2 pr-4 text-white">{row.model_name || "unknown"}</td>
                        <td className="py-2 pr-4">{row.task || "unknown"}</td>
                        <td className="py-2 pr-4 text-right">{brier(row.mean_brier)}</td>
                        <td className="py-2 pr-4 text-right text-soft">
                          {baselineOf(row).toFixed(2)}
                        </td>
                        <td
                          className={`py-2 pr-4 text-right ${
                            row.excess_over_baseline == null
                              ? "text-soft"
                              : row.excess_over_baseline > 0
                                ? "text-red-400"
                                : "text-green-400"
                          }`}
                        >
                          {signed(row.excess_over_baseline)}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {row.samples}
                          {row.samples < (driftMeta?.min_samples ?? 20) && (
                            <span className="text-soft"> (min {driftMeta?.min_samples ?? 20})</span>
                          )}
                        </td>
                        <td className="py-2 pr-4 text-right">
                          {row.slope_per_day == null ? "—" : row.slope_per_day.toFixed(4)}
                        </td>
                        <td className="py-2">
                          <DriftBadge row={row} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <p className="text-soft text-sm">
              No model/task pairs recorded in this window yet — drift needs resolved verdicts.
            </p>
          )}
        </div>
      )}

      {/* Prompt versions (plan 04) */}
      <div className="surface-panel p-5">
        <h3 className="text-base font-semibold text-white mb-1">Prompt versions</h3>
        <p className="text-soft text-xs mb-3">
          Calibration grouped by the prompt-version content hash recorded on each verdict, best mean
          Brier first — the answer to "did the last prompt change help?". Verdicts written before
          prompt versioning stay in the {UNVERSIONED_LABEL} group.
        </p>
        {promptVersions.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="table-theme w-full text-sm">
              <thead>
                <tr>
                  <th className="text-left py-2 pr-4">Prompt version</th>
                  <th className="text-right py-2 pr-4">Verdicts</th>
                  <th className="text-right py-2 pr-4">Resolved</th>
                  <th className="text-right py-2 pr-4">Mean Brier</th>
                  <th className="text-right py-2">vs baseline</th>
                </tr>
              </thead>
              <tbody>
                {promptVersions.map((version) => (
                  <tr key={version.prompt_version}>
                    <td className="py-2 pr-4">
                      <span className="mono text-white" title={version.prompt_version}>
                        {promptVersionLabel(version.prompt_version)}
                      </span>
                      {version.prompt_version === UNVERSIONED_LABEL && (
                        <span className="chip chip-warning ml-2">pre-versioning</span>
                      )}
                    </td>
                    <td className="py-2 pr-4 text-right">{version.count}</td>
                    <td className="py-2 pr-4 text-right">{version.resolved}</td>
                    <td className="py-2 pr-4 text-right">{brier(version.mean_brier)}</td>
                    <td className="py-2 text-right">
                      {version.mean_brier == null ? (
                        <span className="text-soft">—</span>
                      ) : version.mean_brier < BASELINE_BRIER ? (
                        <span className="text-green-400">beats baseline</span>
                      ) : (
                        <span className="text-red-400">worse than baseline</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-soft text-sm">
            No verdicts in this window yet — prompt versions accumulate from deployment onward.
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
                  <th className="pb-2 pr-4">Prompt</th>
                  <th className="pb-2 pr-4">AI prob</th>
                  <th className="pb-2 pr-4">Price</th>
                  <th className="pb-2 pr-4">Edge</th>
                  <th className="pb-2 pr-4">Resolved</th>
                  <th className="pb-2 pr-4">Brier</th>
                  {canRateVerdicts && <th className="pb-2">Your rating</th>}
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
                    <td className="py-2 pr-4 mono" title={forecast.prompt_version ?? "unversioned"}>
                      {forecast.prompt_version
                        ? truncate(forecast.prompt_version, 12)
                        : UNVERSIONED_LABEL}
                    </td>
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
                    <td className="py-2 pr-4">{brier(forecast.brier_score)}</td>
                    {canRateVerdicts && (
                      <td className="py-2 align-top">
                        {forecast.id != null && (
                          <VerdictFeedbackControl
                            assessmentId={forecast.id}
                            contextLabel={`verdict for market ${shortMarket(forecast.condition_id)}`}
                          />
                        )}
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-soft text-sm">No forecasts recorded in this window yet.</p>
        )}
        {!canRateVerdicts && (
          <p className="text-muted text-xs mt-3">
            Verdict feedback (1–5 stars plus an optional comment, wired in{" "}
            <span className="mono">VerdictFeedbackControl</span>) needs each verdict's{" "}
            <span className="mono">id</span> —{" "}
            <span className="mono">GET /api/analysis/calibration/forecasts</span> does not serialise
            it yet while the rating endpoints take it as <span className="mono">assessment_id</span>
            . Adding <span className="mono">"id": row.id</span> to that payload turns this column
            on.
          </p>
        )}
      </div>

      {/* A/B experiments (admin only, plan 04) */}
      {isAdmin && (
        <div className="surface-panel p-5">
          <h3 className="text-base font-semibold text-white mb-1">A/B experiments</h3>
          <p className="text-soft text-xs mb-3">
            Deterministic traffic split across a control and a variant config per task. One active
            experiment per task; the comparison aggregates the resolved verdicts written under each
            arm.
          </p>
          <ExperimentAdminSection windowDays={windowDays} />
        </div>
      )}
    </div>
  );
}

function DriftBadge({ row }: { row: CalibrationDriftRow }) {
  if (row.drifted) {
    return (
      <span className="chip chip-danger" title={row.reasons.join(", ")}>
        DRIFT
      </span>
    );
  }
  if (row.reasons.includes("insufficient_samples")) {
    return (
      <span className="chip chip-warning" title="Not enough resolved verdicts to flag">
        LOW SAMPLES
      </span>
    );
  }
  return <span className="chip chip-success">OK</span>;
}

function configSummary(config: ExperimentConfig): string {
  if (config == null) return "—";
  const entries = Object.entries(config);
  if (entries.length === 0) return "—";
  return entries
    .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
    .join(", ");
}

const EMPTY_ARM = {
  samples: 0,
  resolved: 0,
  mean_brier: null,
  best_brier: null,
  worst_brier: null,
};

function verdictHeadline(report: ExperimentComparisonResponse): string {
  switch (report.verdict) {
    case "variant_better":
      return "Variant better";
    case "control_better":
      return "Control better";
    case "no_difference":
      return "No difference";
    case "insufficient_samples": {
      const min = report.min_resolved_for_verdict ?? MIN_RESOLVED_FOR_VERDICT;
      return `Insufficient samples (needs ${min} resolved per arm)`;
    }
    default:
      return "Inconclusive";
  }
}

function verdictDetail(report: ExperimentComparisonResponse): string {
  switch (report.verdict) {
    case "variant_better":
      return "The variant arm's mean Brier is at least 0.01 lower than control — lower Brier is better.";
    case "control_better":
      return "The variant arm's mean Brier is at least 0.01 higher than control — keep the control config.";
    case "no_difference":
      return "Both arms score within 0.01 Brier of each other — not enough signal to prefer either.";
    case "insufficient_samples":
      return "An arm has fewer resolved verdicts than the gate, so a winner would be noise.";
    default:
      return "At least one arm has no resolved verdicts in this window yet.";
  }
}

function ExperimentAdminSection({ windowDays }: { windowDays: number }) {
  const toast = useToast();
  const [experiments, setExperiments] = useState<LLMExperiment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [task, setTask] = useState("");
  const [trafficPct, setTrafficPct] = useState(10);
  const [controlJson, setControlJson] = useState("{}");
  const [variantJson, setVariantJson] = useState("{}");
  const [creating, setCreating] = useState(false);

  const [comparison, setComparison] = useState<ExperimentComparisonResponse | null>(null);
  const [comparisonError, setComparisonError] = useState<string | null>(null);
  const [comparing, setComparing] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const response = await calibrationAdminService.listExperiments();
      setExperiments(response.experiments);
      setError(null);
    } catch (err) {
      setError(getApiErrorMessage(err, "Could not load experiments"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const parseConfig = (raw: string): ExperimentConfig => {
    const trimmed = raw.trim();
    if (!trimmed) return null;
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed == null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("Config must be a JSON object");
    }
    return parsed as Record<string, unknown>;
  };

  const create = useCallback(async () => {
    setCreating(true);
    setError(null);
    let controlConfig: ExperimentConfig;
    let variantConfig: ExperimentConfig;
    try {
      controlConfig = parseConfig(controlJson);
      variantConfig = parseConfig(variantJson);
    } catch {
      setError("Both configs must be valid JSON objects (or empty).");
      setCreating(false);
      return;
    }
    try {
      await calibrationAdminService.createExperiment({
        name: name.trim(),
        task: task.trim(),
        traffic_pct: trafficPct,
        control_config: controlConfig,
        variant_config: variantConfig,
      });
      toast.success(`Experiment "${name.trim()}" created on task "${task.trim()}".`);
      setName("");
      setTask("");
      setTrafficPct(10);
      setControlJson("{}");
      setVariantJson("{}");
      await refresh();
    } catch (err) {
      setError(getApiErrorMessage(err, "Could not create the experiment"));
    } finally {
      setCreating(false);
    }
  }, [controlJson, name, refresh, task, trafficPct, variantJson]);

  const changeStatus = useCallback(
    async (experiment: LLMExperiment, status: ExperimentStatus) => {
      setError(null);
      try {
        await calibrationAdminService.setExperimentStatus(experiment.id, status);
        toast.success(`"${experiment.name}" is now ${EXPERIMENT_STATUS_LABELS[status] ?? status}.`);
        await refresh();
      } catch (err) {
        setError(getApiErrorMessage(err, "Could not update the experiment"));
      }
    },
    [refresh],
  );

  const compare = useCallback(
    async (experimentName: string) => {
      setComparing(experimentName);
      setComparison(null);
      setComparisonError(null);
      try {
        const report = await calibrationAdminService.getExperimentComparison(experimentName, {
          window_days: windowDays,
        });
        setComparison(report);
      } catch (err) {
        setComparisonError(getApiErrorMessage(err, "Could not load the comparison"));
      } finally {
        setComparing(null);
      }
    },
    [windowDays],
  );

  return (
    <div className="space-y-4">
      {error && (
        <Banner variant="error" onDismiss={() => setError(null)} onRetry={refresh}>
          {error}
        </Banner>
      )}

      {experiments.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="table-theme w-full text-sm">
            <thead>
              <tr>
                <th className="text-left py-2 pr-4">Name</th>
                <th className="text-left py-2 pr-4">Task</th>
                <th className="text-right py-2 pr-4">Traffic</th>
                <th className="text-left py-2 pr-4">Status</th>
                <th className="text-left py-2 pr-4">Control</th>
                <th className="text-left py-2 pr-4">Variant</th>
                <th className="text-left py-2">Actions</th>
              </tr>
            </thead>
            <tbody>
              {experiments.map((experiment) => (
                <tr key={experiment.id}>
                  <td className="py-2 pr-4 text-white">{experiment.name}</td>
                  <td className="py-2 pr-4 mono">{experiment.task}</td>
                  <td className="py-2 pr-4 text-right">{experiment.traffic_pct}%</td>
                  <td className="py-2 pr-4">
                    <span
                      className={`chip ${
                        experiment.status === "active"
                          ? "chip-success"
                          : experiment.status === "paused"
                            ? "chip-warning"
                            : "chip-accent"
                      }`}
                    >
                      {EXPERIMENT_STATUS_LABELS[experiment.status] ?? experiment.status}
                    </span>
                  </td>
                  <td
                    className="py-2 pr-4 max-w-[220px] truncate text-soft"
                    title={configSummary(experiment.control_config)}
                  >
                    {configSummary(experiment.control_config)}
                  </td>
                  <td
                    className="py-2 pr-4 max-w-[220px] truncate text-soft"
                    title={configSummary(experiment.variant_config)}
                  >
                    {configSummary(experiment.variant_config)}
                  </td>
                  <td className="py-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <button
                        type="button"
                        onClick={() => void compare(experiment.name)}
                        disabled={comparing != null}
                        className="btn-muted text-xs px-2 py-1"
                      >
                        {comparing === experiment.name ? "Loading…" : "Compare arms"}
                      </button>
                      {experiment.status !== "done" && (
                        <>
                          {experiment.status === "active" ? (
                            <button
                              type="button"
                              onClick={() => void changeStatus(experiment, "paused")}
                              className="btn-muted text-xs px-2 py-1"
                            >
                              Pause
                            </button>
                          ) : (
                            <button
                              type="button"
                              onClick={() => void changeStatus(experiment, "active")}
                              className="btn-muted text-xs px-2 py-1"
                            >
                              Resume
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => void changeStatus(experiment, "done")}
                            className="btn-danger text-xs px-2 py-1"
                          >
                            Retire
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-soft text-sm">
          {loading ? "Loading experiments..." : "No experiments yet — create one below."}
        </p>
      )}

      {comparisonError && (
        <Banner variant="error" onDismiss={() => setComparisonError(null)}>
          {comparisonError}
        </Banner>
      )}

      {comparison && (
        <div className="surface-soft p-4 space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-sm font-semibold text-white">
              Arm comparison — {comparison.experiment} ({comparison.window_days}d)
            </h4>
            <button
              type="button"
              onClick={() => setComparison(null)}
              className="btn-muted text-xs px-2 py-1"
            >
              Close
            </button>
          </div>
          <p className="text-sm font-bold text-white">{verdictHeadline(comparison)}</p>
          <p className="text-xs text-soft">{verdictDetail(comparison)}</p>
          <div className="overflow-x-auto">
            <table className="table-theme w-full text-sm">
              <thead>
                <tr>
                  <th className="text-left py-2 pr-4">Arm</th>
                  <th className="text-right py-2 pr-4">Samples</th>
                  <th className="text-right py-2 pr-4">Resolved</th>
                  <th className="text-right py-2 pr-4">Mean Brier</th>
                  <th className="text-right py-2 pr-4">Best</th>
                  <th className="text-right py-2">Worst</th>
                </tr>
              </thead>
              <tbody>
                {(["control", "variant"] as const).map((arm) => {
                  const stats = comparison.arms[arm] ?? EMPTY_ARM;
                  return (
                    <tr key={arm}>
                      <td className="py-2 pr-4 text-white capitalize">{arm}</td>
                      <td className="py-2 pr-4 text-right">{stats.samples}</td>
                      <td className="py-2 pr-4 text-right">{stats.resolved}</td>
                      <td className="py-2 pr-4 text-right">{brier(stats.mean_brier)}</td>
                      <td className="py-2 pr-4 text-right">{brier(stats.best_brier)}</td>
                      <td className="py-2 text-right">{brier(stats.worst_brier)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Create form */}
      <div className="surface-soft p-4 space-y-3">
        <h4 className="text-sm font-semibold text-white">New experiment</h4>
        <div className="grid gap-3 sm:grid-cols-3">
          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="experiment-name">
              Name
            </label>
            <input
              id="experiment-name"
              className="input-field w-full"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="market-analysis-v2"
              maxLength={64}
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="experiment-task">
              Task
            </label>
            <input
              id="experiment-task"
              className="input-field w-full"
              value={task}
              onChange={(event) => setTask(event.target.value)}
              placeholder="market_analysis"
              maxLength={50}
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="experiment-traffic">
              Variant traffic (%)
            </label>
            <input
              id="experiment-traffic"
              type="number"
              min={1}
              max={100}
              className="input-field w-full"
              value={trafficPct}
              onChange={(event) => setTrafficPct(Number(event.target.value) || 0)}
            />
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="experiment-control">
              Control config <span className="text-muted font-normal">(JSON)</span>
            </label>
            <textarea
              id="experiment-control"
              rows={3}
              className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm mono focus:border-[var(--accent)] focus:outline-none"
              value={controlJson}
              onChange={(event) => setControlJson(event.target.value)}
              placeholder='{"model": "gpt-4o-mini"}'
            />
          </div>
          <div>
            <label className="block text-sm font-medium mb-1" htmlFor="experiment-variant">
              Variant config <span className="text-muted font-normal">(JSON)</span>
            </label>
            <textarea
              id="experiment-variant"
              rows={3}
              className="w-full px-3 py-2 rounded-md bg-[var(--bg-soft)] border border-[var(--line)] text-white text-sm mono focus:border-[var(--accent)] focus:outline-none"
              value={variantJson}
              onChange={(event) => setVariantJson(event.target.value)}
              placeholder='{"model": "claude-3-5-sonnet"}'
            />
          </div>
        </div>
        <p className="text-xs text-muted">
          Both configs are optional; leave them as <span className="mono">{"{}"}</span> to compare
          the default routing against itself. A second active experiment on the same task is
          rejected with a 409.
        </p>
        <button
          type="button"
          onClick={() => void create()}
          disabled={creating || name.trim().length < 3 || task.trim().length < 1 || trafficPct < 1}
          className="btn-primary text-sm"
        >
          {creating ? "Creating…" : "Create experiment"}
        </button>
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
