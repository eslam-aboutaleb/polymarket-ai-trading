# Event correlation risk — event-correlation-risk-v1

## Objective and applicability

Detect hidden event, underlying-asset, and oracle concentration across open
positions, unsettled payouts, and pending orders. Computes scenario-based
worst-case portfolio loss without assuming independent Bernoulli diversification
or double-counting complementary `YES`+`NO` hedges.

Strictly `advisory_only` (`execution_eligible = false`). Authoritative risk
gates remain in `pre_trade_gate.py` and `live_trading_policy.py`.

## Mechanism and interpretation

1. Group legs by `event_id`, `underlying_asset`, `oracle`, or `condition_id`.
2. For single conditions, evaluate exact `{YES, NO}` terminal payoffs so
   complementary `YES`+`NO` legs offset rather than double-count loss.
3. For `mutually_exclusive` baskets, enumerate feasible joint states (at most one
   winner) rather than summing independent losses.
4. For correlated contracts without a verified diversifying relation (e.g. two
   BTC UP contracts), sum worst-case losses conservatively.
5. Include `pending_order` legs in `total_pending_notional` and `cash_tied_until_settlement`.

## Invocation and examples

- Backend: `POST /api/risk/event-correlation/analyze`, `GET /api/risk/event-correlation/schema`
  (gated by `EVENT_CORRELATION_RISK_ENABLED`, authenticated, read-only).
- Research MCP: `event_correlation_risk`, `describe_event_correlation_risk`.

## Recorded account evidence and scope

`GET /api/risk/event-correlation/recorded` reads the account selected by the existing strict `X-Account-Id` ownership guard. It extends `portfolio_risk_service.py` through a read-only adapter over `UserTrade`, durable `LiveOrder` reservations and recorded rule versions. It creates no account, order or ledger event and requests no wallet, signing or trading API credentials.

The scope is **application-recorded executions and reservations**, not complete wallet reconciliation. Unrecorded external wallet activity is explicitly excluded. The existing wallet report and authoritative account/trading risk gates retain their responsibilities. A missing field is not silently treated as zero: unknown fills, non-durable pending quantities, unreconciled SELLs, unsupported redemption/lifecycle states, future mutations and rule captures older than five minutes abstain with null monetary risk values. Confirmed BUY/SELL fills are netted by token; canceled orders with subsequently confirmed fills retain those filled shares. Unknown pending BUY fees use an explicit conservative upper bound; they are not reported as fees actually paid.

The POST `/analyze` interface is a **declared hypothetical scenario**. Caller-provided legs and relations cannot establish account ownership, verified facts or authority to trade. The recorded reader grants no cross-condition diversification from unverified relations. Missing verified graph relations therefore retain conservative bounds. Expected shortfall remains null without an independently supported joint probability distribution.

## Feasible scenarios and concentration

All declared identical, complementary, mutually-exclusive and directional implication relations are checked for consistency with a bounded iterative 2-SAT pass. Small portfolios enumerate feasible joint binary outcomes; portfolios above twelve conditions sum per-condition worst losses conservatively. Mutual exclusion does not imply that somebody must win. Confirmed same-condition YES/NO holdings may offset; unfilled pending shares branch independently between fill and no fill and cannot supply guaranteed profitable hedge proceeds.

Condition, event, underlying, oracle, family, jurisdiction and resolution-time groups can overlap. Portfolio loss is calculated once, not summed across overlapping labels. Distinct event IDs cannot hide shared BTC/oracle concentration. Concentration comparisons use unrounded ratios at the boundary; displayed percentages are rounded separately. The gross-cost stress bound covers oracle-outage/zero-payout uncertainty without inventing scenario probabilities.

## Discovery, evidence and promotion

Research MCP exposes the hypothetical `event_correlation_risk` and the account-scoped `recorded_event_correlation_risk`, with matching schema descriptors. Shared `ResearchMCPClient.describe_prediction_market_skills()` and `prediction_market_skill()` provide structured entry points for Research, Analysis and orchestrator callers across the related rule, valuation, oracle and event skills. Only fixed repository-owned read-only names are allowed; an execution-capable or wrong-version response is rejected. No foreign description can register an executable plugin or request wallet keys.

`shadow-baseline.json` is reproduced by `scripts/event-risk-shadow.py`. It compares condition-only versus grouped concentration on forward-recorded quotes from the same settlement window. The calculation uses actual token IDs, prices, fee parameters and content hashes, with explicitly counterfactual ten-share holdings. It is a diagnostic replay, not a precommitted trading backtest or a claim of actual fills. The shared raw dataset and its source/time limitations remain available for independent replication.

Unit/property tests independently simulate settlement/fill branches and check the upper bound, including partial/unfilled hedges, large inconsistent graphs, duplicate identities, coherent implications, stale data and no false diversification. PostgreSQL API replay exercises account isolation and stored fills/reservations. Initial status is implemented detector; human approval, full wallet reconciliation and supported empirical probability/coverage evidence are prerequisites for any future production promotion. Monitor source freshness, unknown lifecycle states, duplicate/conflicting identities and concentration warnings. Roll back with `EVENT_CORRELATION_RISK_ENABLED=false`; this flag defaults off and cannot override pre-trade caps, the durable gateway or emergency halt.

The pure Decimal schemas/scenario arithmetic can migrate through a versioned shared financial-skills adapter; account ownership, storage, rules and venue joins remain application-owned. References are the repository's existing `portfolio_risk_service.py`, durable `execution/live.py` contract and immutable public-data replay. No new signing or execution dependency is introduced.
