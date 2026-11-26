/**
 * Scorecard for one evidence-grounded forecast (issue #4).
 *
 * Shows the forecast next to the implied market price, the uncertainty when the caller has the
 * estimate interval, and every source with its publication and retrieval time so the number is
 * traceable. Read-only and research-only: there is no order action and the backend always
 * returns `execution_eligible=false`.
 *
 * @module components/ForecastScorecard
 */
import { FormEvent, useState } from "react";
import { getApiErrorMessage, isApiError } from "../utils/apiError";
import {
  ForecastEvidenceResponse,
  forecastGenerationService,
} from "../services/calibrationService";

const pct = (value: number | null | undefined) =>
  value == null || Number.isNaN(value) ? "—" : `${(value * 100).toFixed(1)}%`;

export function edgeLabel(forecast: number | null, market: number | null): string {
  if (forecast == null || market == null) return "No implied price to compare";
  const points = (forecast - market) * 100;
  const sign = points > 0 ? "+" : "";
  return `${sign}${points.toFixed(1)} points vs market (not a trade signal)`;
}

export const formatTime = (value: string | null) =>
  value ? new Date(value).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "undated";

interface Props {
  initialForecastId?: string;
  interval?: [number, number] | null;
}

export default function ForecastScorecard({ initialForecastId = "", interval = null }: Props) {
  const [forecastId, setForecastId] = useState(initialForecastId);
  const [data, setData] = useState<ForecastEvidenceResponse | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [disabled, setDisabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = async (event: FormEvent) => {
    event.preventDefault();
    const id = forecastId.trim();
    if (!id) return;
    setLoading(true);
    setError(null);
    setDisabled(false);
    try {
      const evidence = await forecastGenerationService.getEvidence(id);
      setData(evidence);
      const calibration = await forecastGenerationService.getCalibration();
      setStatus(calibration.report.status);
    } catch (err: unknown) {
      setData(null);
      setStatus(null);
      if (isApiError(err) && err.status === 503) setDisabled(true);
      else if (isApiError(err) && err.status === 404) setError("No such forecast or evidence.");
      else setError(getApiErrorMessage(err, "Could not load the forecast evidence"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="surface-panel p-5" data-testid="forecast-scorecard">
      <h3 className="text-base font-semibold text-white mb-1">Forecast scorecard</h3>
      <p className="text-soft text-xs mb-3">
        A forecast, the market price it is compared with, and the evidence behind it. Research only:
        no order action, and a gap to the market is not a trade signal.
      </p>
      <form onSubmit={load} className="flex gap-2 mb-3">
        <input
          aria-label="Forecast id"
          className="flex-1 px-2 py-1 rounded bg-surface border border-border text-sm text-white"
          value={forecastId}
          onChange={(e) => setForecastId(e.target.value)}
          placeholder="Forecast id"
        />
        <button type="submit" className="btn-secondary px-3 py-1 text-sm" disabled={loading}>
          {loading ? "Loading..." : "Show evidence"}
        </button>
      </form>
      {disabled && (
        <p className="text-soft text-sm" data-testid="scorecard-disabled">
          Forecast generation is switched off (FORECAST_GENERATION_ENABLED) or needs an admin
          account.
        </p>
      )}
      {error && (
        <p className="text-red-400 text-sm" role="alert">
          {error}
        </p>
      )}
      {data && (
        <div className="space-y-3">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm text-white">
            <div data-testid="scorecard-forecast">Forecast {pct(data.forecast_probability)}</div>
            <div data-testid="scorecard-market">Market {pct(data.market_price)}</div>
            <div data-testid="scorecard-interval">
              Interval {interval ? `${pct(interval[0])} to ${pct(interval[1])}` : "not recorded"}
            </div>
            <div data-testid="scorecard-skill">Skill: {status ?? "unknown"}</div>
          </div>
          <p className="text-soft text-xs" data-testid="scorecard-edge">
            {edgeLabel(data.forecast_probability, data.market_price)}
          </p>
          <p className="text-soft text-xs">
            Rule {data.rule_hash ?? "unverified"}, snapshot {data.evidence_snapshot_id.slice(0, 12)}
            , {data.evidence.independent_sources} independent sources, predicted{" "}
            {formatTime(data.prediction_instant)}.
          </p>
          <ul className="space-y-2" data-testid="scorecard-sources">
            {data.evidence.kept.map((item) => (
              <li key={item.source_id} className="text-xs text-soft">
                <span className="text-white">{item.domain}</span> {item.title}
                <br />
                published {formatTime(item.published_at)}, retrieved {formatTime(item.retrieved_at)}
              </li>
            ))}
          </ul>
          {data.evidence.quarantined_source_ids.length > 0 && (
            <p className="text-yellow-400 text-xs" data-testid="scorecard-quarantined">
              {data.evidence.quarantined_source_ids.length} source(s) quarantined as unsafe text and
              not used.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
