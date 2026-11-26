# Promotion gate: paper to live

No strategy has shown a profit in this repo (see the 13 November replay and the maker baseline,
both with no profitability claim). The promotion gate is a rule that live enablement cannot
skip. **Passing it does not imply a strategy is or will be profitable.** It only removes
strategies that have no evidence.

## Criteria (per strategy: latency_arb, maker, copy, inverse, llm_sized)

1. **Frozen parameters.** The parameter set is hashed (SHA-256 of canonical JSON). The test
   window starts when the hash is first recorded. Any change gives a new hash and restarts the
   clock. Trades under other hashes, or resolved before the window, are excluded and counted.
2. **Sample.** At least `min_trades` resolved paper trades inside the window (default 300).
3. **After-fee result.** Mean net return per dollar with a percentile bootstrap 95% CI
   (2000 resamples, fixed seed). The CI lower bound must be above zero.
4. **Baseline.** For forecast strategies (`require_baseline`), Brier score must beat the market
   midpoint. Log-loss is reported too.
5. **Report.** N, gross mean, mean fee, net mean, CI, baseline and an evidence hash are in
   `PromotionDecision.to_dict()`. Fees are shown separately from gross return.

`live_trading_policy.require_promotion_gate(strategy, decision)` raises `LiveTradingBlocked`
with the unmet criteria when the evidence is missing, belongs to another strategy, or did not
pass. It is an extra barrier. `block_legacy_live_execution` and the durable gateway still apply.

## Tiny live-cap stage and demotion

After promotion, live runs under a `LiveCap` (max notional per trade, max daily loss).
`evaluate_demotion` returns to paper when the cap is breached, the daily loss limit is hit, or
live mean net return (at least 30 live trades) falls below the paper CI lower bound.

## Not covered yet

The module takes resolved trades as input. It is not yet fed from the paper journal, the
decision is not stored in the database, and the live-cap limits are not enforced by the
execution path. DSR/PBO checks are not combined into the decision.
