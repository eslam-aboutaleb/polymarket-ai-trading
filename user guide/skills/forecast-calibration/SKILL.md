# Forecast calibration, forecast-calibration-v1

## Objective and applicability

Score the application's stated P(YES) forecasts with proper scoring rules, compare
them with the market-implied probability observed at the same timestamp, and say
plainly when the evidence is too thin to claim skill. Applies to binary YES/NO
Polymarket markets with a finalized settlement. Not applicable to multi-outcome,
scalar, void, disputed or unresolved markets (they are excluded with a reason code).

Strictly `advisory_only` and research-only (`execution_eligible = false`). It holds no
wallet or broker credentials and cannot sign, submit or cancel orders. The authoritative
gates stay in `pre_trade_gate.py` and `live_trading_policy.py`.

## Prerequisites

Forecasts recorded by `forecast_recorder.py` with the identity and benchmark columns of
issue #60 (request key, token, rule hash, prediction instant, observed-at benchmark,
event group) and scored by `forecast_resolution.py`. No second forecast database exists.

## Mechanism

1. Brier `(p-y)^2`, exact. Log loss clips `p` to `[1e-6, 1-1e-6]` inside the logarithm only;
   the policy and the number of clipped forecasts are reported.
2. Inclusion: finite `p` in `[0,1]`, a finalized binary label, a known prediction instant
   strictly before the market close, no reversed token mapping. Every drop is counted by
   reason; nothing is imputed (never 0.5).
3. Independence: forecasts on one event are one trial. The unit is the event cluster (mean
   of its forecast scores). Uncertainty is a seeded percentile bootstrap over clusters.
4. Calibration: reliability bins with per-bin count, event support and Wilson 95% interval;
   ECE with 5, 10 and 20 bins (sensitivity); calibration-in-the-large; sharpness.
5. Baseline: the market price observed at or before the forecast. A price without an
   observation time, a stale one, or one observed after the forecast is not a matched
   baseline and never enters the paired delta. Delta is baseline minus model, so positive
   means the model did better; Brier skill is `1 - model/market` when the market Brier is above 0.
6. Recalibration (isotonic, Platt) is fitted only on forecasts whose label was known by a
   cutoff and evaluated on forecasts made after it. A label that arrived after the cutoff
   (delayed or disputed) is in neither set. Beta calibration is not implemented: with the
   sample sizes in this application it is not justified.
7. Status: `better_than_market` only when the 95% interval of the paired Brier delta is above
   zero AND the number of matched independent events reaches `min_effective_events` (30 by
   default). Otherwise `insufficient_sample`, `not_distinguishable`, `worse_than_market` or
   `no_matched_baseline`. Model drift (`model_drift.detect_drift`) forces research-only.

## Invocation

- Backend: `GET /api/analysis/calibration/skill-report` (authenticated, read-only,
  `FORECAST_CALIBRATION_ENABLED`, default off; `scope=global` is admin only).
- Research MCP: `forecast_calibration`, `describe_forecast_calibration`.
- Resolution audit: `GET /api/analysis/calibration/forecasts/{id}/history` (admin).

## Interpretation and limits

A good Brier is not alpha. The report sets `claims_alpha = false` and the evidence class is
`implemented_detector`. Fees, slippage and executability are not part of the score; they
belong to the sizing and execution gates. Results from a window with few events are
descriptive only.

## Failure modes

Missing baseline observation time (baseline unverified), thin cohorts, clustered events,
changed rules or reversed token mapping after the forecast (excluded), disputed or late
settlement (excluded or quarantined by the resolution audit), probability exactly 0 or 1
(kept as stated, clipped only in the logarithm).

## References

Scoring rules: https://en.wikipedia.org/wiki/Scoring_rule .
scikit-learn calibration: https://scikit-learn.org/stable/modules/calibration.html .
Brier score: https://scikit-learn.org/stable/modules/generated/sklearn.metrics.brier_score_loss.html .
Polymarket Gamma API: https://gamma-api.polymarket.com/docs .

## Promotion, rollout and rollback

Status is implemented detector. Promotion needs the human-review checklist below and
forward-recorded matched baselines on disjoint future events. Roll out read-only, then
shadow. Roll back with `FORECAST_CALIBRATION_ENABLED=false`; the flag cannot touch order
submission, the durable gateway, risk caps or the emergency halt.

Human review checklist: matched-baseline coverage above 80% of scored forecasts; at least 30
independent events per cohort; frozen hypotheses; independent replication of the report from
the stored rows; drift state clear.

Future shared-skills mapping: the pure functions in `forecast_scoring.py` (Brier, log loss,
reliability, bootstrap, isotonic, Platt) can move to the financial-skills SDK behind a versioned
import adapter. The Gamma/CLOB token mapping, rule hashes and storage stay application-owned.
