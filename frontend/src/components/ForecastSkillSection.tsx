/**
 * Forecast skill vs the matched market baseline (issue #22).
 *
 * Reads `/api/analysis/calibration/skill-report`: paired Brier and log-loss
 * deltas with event-cluster bootstrap intervals, ECE, effective independent
 * events, source coverage, and a per-model research-only state. It never
 * claims alpha and never offers an order action: the report is research-only
 * and `execution_eligible` is always false on the backend.
 *
 * @module components/ForecastSkillSection
 */
import { useEffect, useState } from "react";
import { isApiError, getApiErrorMessage } from "../utils/apiError";
import {
  ConfidenceInterval,
  ForecastSkillCohort,
  ForecastSkillReport,
  ForecastSkillStatus,
  calibrationService,
} from "../services/calibrationService";

export const STATUS_LABELS: Record<ForecastSkillStatus, string> = {
  BETTER_THAN_MARKET: "Better than market (CI excludes 0)",
  WORSE_THAN_MARKET: "Worse than market (CI excludes 0)",
  NOT_DISTINGUISHABLE: "Not distinguishable from market",
  INSUFFICIENT_SAMPLE: "Insufficient independent events",
  NO_MATCHED_BASELINE: "No matched market baseline",
};

const num = (value: number | null | undefined, digits = 4) =>
  value == null || Number.isNaN(value) ? "—" : value.toFixed(digits);

export const formatInterval = (ci: ConfidenceInterval | undefined, digits = 4) =>
  ci == null ? "—" : `[${ci[0].toFixed(digits)}, ${ci[1].toFixed(digits)}]`;

export function cohortSummary(cohort: ForecastSkillCohort): string {
  const needed = cohort.min_effective_events;
  const have = cohort.paired_events ?? cohort.effective_independent_events;
  if (cohort.status === "INSUFFICIENT_SAMPLE") {
    return `${have} of ${needed} independent events needed before skill is reported`;
  }
  return `${have} independent events (minimum ${needed})`;
}

interface Props {
  windowDays: number;
  scope: "user" | "global";
}

export default function ForecastSkillSection({ windowDays, scope }: Props) {
  const [report, setReport] = useState<ForecastSkillReport | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setDisabled(false);
    calibrationService
      .getSkillReport({ window_days: windowDays, scope })
      .then((data) => {
        if (!cancelled) setReport(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setReport(null);
        if (isApiError(err) && err.status === 503) setDisabled(true);
        else setError(getApiErrorMessage(err, "Could not load the forecast skill report"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [windowDays, scope]);

  const overall = report?.overall;
  const models = Object.entries(report?.by_model ?? {});
  const horizons = Object.entries(report?.by_horizon ?? {});

  return (
    <div className="surface-panel p-5" data-testid="forecast-skill-section">
      <h3 className="text-base font-semibold text-white mb-1">Forecast skill vs market</h3>
      <p className="text-soft text-xs mb-3">
        Paired against the market price observed at the forecast instant, resampled by event so
        overlapping markets in one event count once. Research only: this never changes sizing or
        orders, and a good score alone is not a claim of alpha.
      </p>
      {loading && <p className="text-soft text-sm">Loading skill report...</p>}
      {disabled && (
        <p className="text-soft text-sm" data-testid="skill-disabled">
          The forecast skill report is switched off (FORECAST_CALIBRATION_ENABLED).
        </p>
      )}
      {error && (
        <p className="text-red-400 text-sm" role="alert">
          {error}
        </p>
      )}
      {report && overall && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <span
              className="px-2 py-1 rounded bg-surface border border-border text-sm text-white"
              data-testid="skill-status"
            >
              {STATUS_LABELS[overall.status] ?? overall.status}
            </span>
            <span className="text-soft text-xs" data-testid="skill-sample">
              {cohortSummary(overall)}
            </span>
            {overall.abstain_reason && (
              <span className="text-yellow-400 text-xs" data-testid="skill-abstain">
                Abstain reason: {overall.abstain_reason}
              </span>
            )}
          </div>
          <dl className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
            <Metric
              label="Paired Brier delta"
              value={num(overall.paired_brier_delta)}
              hint={`95% CI ${formatInterval(overall.paired_brier_delta_ci95)}`}
            />
            <Metric
              label="Paired log-loss delta"
              value={num(overall.paired_log_loss_delta)}
              hint={`95% CI ${formatInterval(overall.paired_log_loss_delta_ci95)}`}
            />
            <Metric
              label="Brier skill vs market"
              value={num(overall.brier_skill, 3)}
              hint={`market Brier ${num(overall.market_brier)}`}
            />
            <Metric
              label="ECE"
              value={num(overall.ece, 3)}
              hint={`calibration-in-the-large ${num(overall.calibration_in_the_large, 3)}`}
            />
          </dl>
          <p className="text-xs text-soft">
            Delta convention:{" "}
            {overall.delta_convention ?? "baseline minus model (positive favours the model)"}.{" "}
            Scored {overall.scored_forecast_count} of {overall.forecast_count} forecasts;{" "}
            {Object.entries(overall.excluded)
              .map(([reason, count]) => `${count} excluded (${reason})`)
              .join(", ") || "none excluded"}
            ; {overall.clipped_for_log_loss} clipped for log loss.
          </p>
          <p className="text-xs text-soft" data-testid="skill-coverage">
            Source coverage: {report.source_coverage.with_matched_baseline} of{" "}
            {report.source_coverage.forecasts} forecasts have a matched market baseline;{" "}
            {report.source_coverage.with_event_group} carry an event group.
          </p>
          {models.length > 0 && (
            <table className="w-full text-sm" aria-label="Skill by model">
              <thead>
                <tr className="text-soft text-xs text-left">
                  <th>Model</th>
                  <th>Status</th>
                  <th>Events</th>
                  <th>Delta CI</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {models.map(([name, cohort]) => (
                  <tr key={name} data-testid={`skill-model-${name}`}>
                    <td className="text-white">{name}</td>
                    <td>{STATUS_LABELS[cohort.status] ?? cohort.status}</td>
                    <td>{cohort.paired_events ?? cohort.effective_independent_events}</td>
                    <td className="mono">{formatInterval(cohort.paired_brier_delta_ci95)}</td>
                    <td>
                      {cohort.state?.research_only
                        ? `Research only (${cohort.state.reasons.join(", ")})`
                        : "Skill demonstrated"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {horizons.length > 0 && (
            <p className="text-xs text-soft">
              By horizon:{" "}
              {horizons
                .map(([name, cohort]) => `${name} ${cohort.effective_independent_events} events`)
                .join(" · ")}
            </p>
          )}
          {report.drift.drifted_models.length > 0 && (
            <p className="text-red-400 text-xs" data-testid="skill-drift">
              Drift detected for {report.drift.drifted_models.join(", ")}: kept research-only.
            </p>
          )}
        </div>
      )}
    </div>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div>
      <dt className="text-xs text-soft">{label}</dt>
      <dd className="text-lg font-bold text-white">{value}</dd>
      <dd className="text-xs text-soft">{hint}</dd>
    </div>
  );
}
