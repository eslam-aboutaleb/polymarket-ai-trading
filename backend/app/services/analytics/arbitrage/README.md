# Arbitrage feasibility engine (issue #9)

Read-only and paper-only. Nothing here signs, submits or cancels an order, and it does not touch
`pre_trade_gate.py` or `live_trading_policy.py`.

## Scanner output (`arb-scan-v2`)

`arbitrage_service` emits **indicative** signals only:

| Field | Meaning |
| --- | --- |
| `signal_class` | `complement_signal` or `spread_signal` (a wide spread is a liquidity signal, not arbitrage) |
| `gross_price_gap_pct_points` | Gross gap between indicative prices and 1.00, in percentage points. Not executable, not fee-adjusted, not a return on capital |
| `profit_pct` | Deprecated alias, same number as before, kept so older clients keep working |
| `badges` | `INDICATIVE_ONLY`, `NOT_EXECUTABLE`, `UNKNOWN_FEES` |

Cross-venue rows (`cross-venue-v2`) are `REVIEW_ONLY` (`NON_EXECUTABLE` for play money) and carry both
venues' questions and rules plus the parity checks that could not be verified.

## Feasibility (`arb-feasibility-v1`)

`POST /api/v1/arbitrage/{candidate_id}/feasibility` (flag `ARBITRAGE_FEASIBILITY_ENABLED`, default off)
reads frozen native book receipts at an explicit `as_of` and returns an `ArbCandidate`:

- States: `INDICATIVE`, `DEPTH_VERIFIED`, `EXECUTION_READY`, `STALE`, `INVALID`, `PARTIAL`.
  `EXECUTION_READY` still means paper research only.
- Payoff matrix over YES / NO / VOID scenarios. Cross-venue legs resolve independently unless the
  event, oracle, cutoff, outcome meaning, currency and settlement are all explicitly equal.
- Both legs are priced by walking real asks at one common size. Quote-gross and fee-inclusive bounds
  are reported separately. Unknown fees or costs are listed, never treated as zero.
- One-leg failure exposure is bounded by the cash spent on the first filled leg; a modeled unwind uses
  displayed bids only.
- `persist: true` freezes the run (account-owned, replayable by `GET /api/v1/arbitrage/runs/{id}`).

## Rollout and rollback

Shadow first: leave the flag off, enable for read-only review, then paper. Live execution is not part
of this issue. Rollback: turn the flag off, or `alembic downgrade 20261122_0042_studies`, which drops
only `arbitrage_feasibility_runs` and leaves book research and historical scans intact.

## Not established

No forward-tested net performance exists. Nothing here claims earnings.
