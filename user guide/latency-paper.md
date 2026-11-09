# Latency arbitrage paper execution

Open **Latency Arb → Paper execution evidence** to inspect every durable paper decision, including decisions which could not execute. History is scoped to the active account, paginated, and survives refresh and server restart.

| Label | Meaning |
| --- | --- |
| Estimated | A modeled IOC sweep against validated, recorded native ask depth. |
| Partial estimate | Only part of the requested cash budget matched displayed depth within the price bound; the rest was canceled. |
| Unfilled | Available evidence showed insufficient capacity or an edge erased by execution costs. |
| Unverifiable | Timing, fees, market identity, native book evidence, or risk authorization could not support an execution estimate. |

Paper executions are **never exchange-confirmed**. No data means no invented entry or realized profit. A model probability is a forecast, not a fill price. The table separates probability, measured delay, estimated shares, cash including fees, and realized paper P&L. Buying inventory does not realize profit. Shared-journal sales and paired final settlement evidence determine realized modeled results. These remain separate from live settled P&L.

Legacy simulated rows without execution provenance are labeled **Unverifiable legacy** and their reported P&L is suppressed in the latency trade API/table. They are not retroactively promoted to proven fills.

## Operator setup

1. Apply migrations with `cd backend && uv run alembic upgrade head`. Revision `20261027_0038_paper_intents` adds the immutable decision table; existing trades and journal entries remain intact.
2. Enable `PAPER_JOURNAL_V2_ENABLED` and `NATIVE_CAPTURE_ENABLED` and enable simulation mode for the owner and selected account.
3. Configure explicit paper fee assumptions and acknowledge exclusion of any unverified legacy history using the [paper accounting guide](paper-accounting.md). Unknown fees abstain; zero fees require explicit configuration.
4. Enable the latency strategy and retain conservative account and strategy limits. Live flags remain off by default and are not required for paper execution.

The runner commits an intent before waiting or fetching public evidence. It freezes the intended token/outcome, cash budget, model probability, minimum edge, measured feed/decision delay, fee and policy assumptions, and deadline. Source and receipt timestamps must both fall after the eligible execution time and no later than the frozen evidence cutoff. A capture received after the strategy deadline cannot fill the intent. Changing fee/policy assumptions while an intent is pending causes abstention instead of improving its historical economics.

Only captured native CLOB snapshots with paired token/condition/rule metadata can support estimates. The shared binary-book validator rejects crossed/equal books, malformed levels, ambiguous receipt ordering, identity conflicts and stale/future clocks. Explicit paired Up/Down labels are mapped to YES/NO without guessing from position. Gamma midpoint prices are never used to fabricate depth.

Each IOC price is bounded by `(model probability - minimum edge) / (1 + assumed fee rate)`, rounded down to the native tick. Fees are included in the authorized cash budget. Shared journal depth consumption prevents repeated strategies from spending the same displayed liquidity. No replenishment, resting queue position, maker probability or unseen order flow is inferred.

After capture, mutable owner/account mode, halt, strategy selection and risk checks run inside the economic transaction. Owner paper limits include all owned paper journals; account limits include the selected journal. Gross negative realized events count toward daily/monthly stops even after earlier gains. Missing valuation evidence blocks new risk. The existing global safety gate also remains in force.

## Recovery and rollback

A canceled task leaves a pending immutable decision with no financial fill. Retrying the same account/condition/window uses that decision, and the first committed shared-journal result is returned exactly. Recovery after its deadline records `INTENT_EXPIRED`; it never backdates a financial fill. A reused identity with different token/cash is rejected.

Disable the strategy or journal flag to stop new activity. Keep the additive intent table when rolling back application code so history is retained. Downgrading revision 0038 deletes decision history only; it does not reverse shared-journal economics. Export needed decisions before any schema downgrade.

The execution model currently replays account journals and scans prior depth budgets. Owner-wide risk serialization and full replay favor financial consistency over throughput; provision and measure account/history size accordingly. Public capture can fail or take longer than the configured deadline, producing abstention. Paper backtesting cannot prove live fills or profitability.

## Verification

`npm run test:latency-paper` exercises the real strategy cycle, API, SQL journal and browser against controlled native receipts, with both private SDKs and the live gateway fenced. Backend regressions cover measured 0/300/1500 ms delays, partial/zero depth, costs, invalid clocks/books, changed assumptions, cancellation, restart, owner/subaccount risk and Decimal conservation. PostgreSQL tests cover simultaneous duplicate intents and additive migration preservation. CI runs these alongside existing paper and live-gateway safety suites.
