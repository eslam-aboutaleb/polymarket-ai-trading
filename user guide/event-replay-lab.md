# Event replay, shadow runs and strategy laboratory (#2)

Research only. Nothing in `backend/app/services/backtesting/` imports a signer, wallet,
order gateway, database session or network client (a subprocess test enforces this). It
cannot place, sign or cancel an order, and live trading policy and gates are untouched.
Results are modeled, never exchange-confirmed. Feature flag: `EVENT_REPLAY_LAB_ENABLED`
(default off).

## Semantics

- **Events** carry `event_time` (source clock) and `available_at` (when we could know it).
  Only `available_at` gates visibility, so a late-delivered observation never informs an
  earlier decision. Events sort by `(available_at, sequence, event_id)`.
- **Latency.** An order arrives `decision_latency + submission_latency + seeded jitter` after
  the decision and matches the book visible at arrival, not the decision book.
- **Equal instants.** Market events apply before order arrivals, then cancels, then expiries.
  The venue is assumed to have moved first.
- **Fills.** Taker fills walk displayed depth, never beyond the limit or displayed size.
  A partial book gives a partial fill. Limits round to tick against the trader.
- **No depth.** A price with no size is capped at `approximation_max_shares`; the fill and the
  run are labeled `approximation_no_depth` / `approximate`.
- **Maker orders** fill only from observed trade flow: a trade through the limit fills at the
  limit; a trade at the limit advances an observed queue and fills only after it clears.
  With no queue evidence the touch is recorded as `maker_fill_uncertain_no_queue_evidence`.
- **Cancel/fill race** resolves by arrival time; a late cancel is rejected, a filled order
  stays filled.
- **Settlement.** Only confirmed FINAL/VOID credits cash. Pending, disputed and unconfirmed
  states never do. Resting orders on a settled token are cancelled, in-flight orders rejected.
- **Costs.** Unknown fee rates are `None`, never zero: net P&L is withheld and the run is
  `approximate`. `fee_schedule_id` and `taker_fee_bps` must be supplied together.
- **P&L** comes only from the ledger of follower fills. A leader's `source_pnl` is ignored.
  Each report proves cash against an independent fill replay and `cash + cost basis =
  funding + realized`.
- **Determinism.** Same events, config, parameters and seed give an identical `result_hash`.

## Laboratory

`walk_forward_validator`: chronological train/validation/holdout split by decision time,
group purging, embargo, and purge of signals whose market resolves after their segment.
`tune()` never reads the holdout; `evaluate_holdout()` works once per validator. Every
attempted candidate is reported. Significance needs 30 independent market groups, uses a
Bonferroni family adjustment and a cluster bootstrap; probability of backtest overfitting
and deflated Sharpe are explicitly not computed.
`strategy_lab`: no-trade, passive-holding and naive zero-latency copy baselines on the same
data and costs, a benchmark report, and a comparison that leaves unknown net P&L unranked.
`shadow_runner`: the same engine over observed events, `orders_signed = 0`, plus a parity
check against paper replay.

## API (account scoped, `X-Account-Id`)

`POST /api/backtesting/lab/event-replays`, `GET .../event-replays/{id}`,
`POST .../event-replays/{id}/verify`, `POST .../shadow-runs`, `GET .../runs`,
`GET .../strategy-lab/compare?run_ids=..`. Runs are stored on the existing `BacktestRun`
record (`strategy_type` `event_replay` / `shadow_run`), so there is no schema change and
nothing to migrate or roll back. Disabling the flag hides the endpoints and keeps history.

## Limits

Inputs are caller-supplied tapes; this feature does not fetch or invent market data. Point
-in-time capture from #1 and the shared ledger from #27 are used by compatible tapes, not
re-implemented. Queue position advances only on observed trades (cancels ahead are not
modeled). No benchmark here shows profitability.
