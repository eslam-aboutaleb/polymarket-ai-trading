# Maker economics — maker-economics-v1

Status: implemented detector, **not validated**. Read-only. No wallet keys, no ability to sign, submit or cancel.

## Objective and applicability
Measure what maker quotes actually earned: realized P&L, fill-weighted effective spread, post-fill markouts,
adverse selection, nonfills and cancel-race losses. It never confuses quoted spread with realized return.
Applies to binary Polymarket markets quoted by `market_maker_service.py`. Not applicable to multi-outcome
forms, combos, or any market without a recorded condition/token identity. Quote policy is owned by #8.

## Mechanism
`maker_realized_pnl = fill cashflow + closing cashflow - maker fees + confirmed rebates - holding costs`.
- Counted only over `venue_confirmed` fills. `modeled` fills appear in `counterfactual_modeled`, never in realized P&L.
- Closing cashflow is the sale proceeds if the inventory was closed, else `inventory * settlement payout`.
- Effective spread = `2 * side * (mid_at_fill - fill_price)`, fill-weighted. Markout at horizon h = `side * (mid_after_h - fill_price)`.
- Adverse-selection rate = share of fills with a negative markout at the middle horizon (10s by default).
- Cancel race: fills that happened after the cancel was requested; reported with their markout loss.
- Nonfills: quotes that ended cancelled/expired/rejected/resting with no fill. Missed-fill value is `null`: it is not observable without venue queue data. No queue priority is assumed.

## Abstentions (no silent zeros)
`STALE_OR_MISSING_EVIDENCE`, `UNKNOWN_MAKER_FEE`, `UNKNOWN_HOLDING_COST`, `UNRESOLVED_INVENTORY`. A quote that abstains is excluded
from realized P&L and its open inventory is reported as `unrealized_inventory_shares`. Rebates count only when `confirmed`.

## Fee and rebate table
Embedded as `FEE_TABLE` with source URLs and dates. Sources: https://docs.polymarket.com/trading/fees,
https://docs.polymarket.com/programs/maker-rebates, https://docs.polymarket.com/programs/fee-and-rebate-changes-november-2026
(read 2026-11-25; effective from 2026-11-20 per that page). Makers pay no fee; takers pay `shares * rate * p * (1-p)`;
rebates are tiered (standard tier starts at $200,000 of 30-day maker weighted volume) and fee-free markets and combos earn none.
Per-market archived fee metadata overrides the category table.

## Invocation
- Backend: `GET /api/market-maker/economics?config_id=<id>` (authenticated, account-scoped, read-only).
- Research MCP: `maker_economics`, `describe_maker_economics` (read-only, `execution_eligible: false`).
- The backend adapter reads the paper maker journal, so every fill is `modeled`; realized P&L stays `null` until venue-confirmed fills are supplied.

## Output
`schema_version`, `as_of` (UTC), `source_ids`, `condition_ids`, `token_ids`, `fee_table_version`, `horizons_seconds`, `realized`,
`venue_confirmed`, `counterfactual_modeled`, `nonfills`, `rebates`, `kill_switch_recommendation`
(`INSUFFICIENT_CONFIRMED_FILLS` | `CONTINUE_SHADOW` | `PAUSE_QUOTING`), `report_hash`.

## Limitations and failure modes
Indicative paper marks are midpoint drift, not realized profit. Mid at fill time is not stored for paper fills, so effective
spread is `null` there. Small samples: the recommendation needs at least 30 confirmed fills.
Evidence status stays `implemented_detector_not_validated` regardless of passing tests.

## Future migration
The module is pure (`app/services/analytics/maker_economics.py`) with typed inputs and a versioned output, so it can move to the
shared skills package behind a thin import adapter. No venue credentials belong in this file or the package.

## Rollout and rollback
Read-only endpoint; nothing to roll back in the order path. Remove the route and MCP entry to disable.
