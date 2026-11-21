# Live accounting evidence — issue #3

This extends the existing execution analytics, FIFO reconciler, UserTrade, and
Execution Analytics screen. It adds read-only observations, attribution and saved
evidence; it does not create another financial ledger or change trading settings.
Issue #3 remains open for the outstanding evidence requirements listed below.

## Rollout and bounds

The diagnostic flag `LIVE_ACCOUNT_RECONCILIATION_ENABLED` defaults to false. Apply
Alembic head before enabling it. Existing trading-enabled defaults and paper/live
selection are unchanged. Disable this flag to hide the diagnostic; preserve
observations and saved reports for audit. Export artifacts before downgrading
the additive v0040/v0041 tables. No financial-history backfill is performed.

Capture is limited to 2/minute, 500 application orders, 20 combined known/discovered
tokens and a 10-second elapsed budget (in-flight SDK calls have their own timeout).
V2 reads allow at most 10 cursor pages, 500 rows per stream and 256 KiB per response.
Reports cap at 5,000 intent/fill audit records and 500 orders. Bounds reject rather
than silently truncate financial evidence. All endpoints authorize the owner and
X-Account-Id. Subaccount/shared-wallet cash capture is rejected until wallet
allocation is proven. Analytics requests bypass the URL-only offline service-worker
cache, and account changes clear stale screen results. No key derivation, arbitrary
URL, signing, submit or cancel call is used.

Tolerance is 0.000001 native collateral units for cash and 0.000001 token shares.
Cash residual = closing cash minus opening cash minus net recorded trading cashflow.
Position residual = closing shares minus opening shares minus signed confirmed fills.
Known indexed funding is disclosed separately from trading cashflow and profit.


The v2 contract reconstructs opening FIFO lots from all earlier confirmed gateway
fills (bounded at 5,000 audit records). Quantity must match the opening native
observation exactly. Historical fills confirmed after the opening watermark are
flagged; they cannot silently repair a previously reviewed result. Entry fees are
carried with the remaining lot and allocated once at exit. Every allocation names
its entry and exit audit source. Reports verify the immutable order-intent identity
before using current order rows, and reject duplicate/conflicting economic fills
before building either totals or benchmarks. Different database timezone settings
preserve the instant through UTC conversion.

The versioned `arrival-midpoint-spread-v1` benchmark uses the existing native-book
archive and its market/rule checks. Both source and receipt clocks must precede
the persisted decision, within 60 seconds. Adverse implementation shortfall equals
spread cost plus the remaining execution difference; none is causal market impact
or an extra deduction from already realized P&L. Future, stale, absent or conflicting
books produce unknown figures. Indicative exit-bid marks use remaining verified
basis and preserve their book/rule references. Venue-final payout metadata is
reported separately from a chain-final redeemed cash receipt; it never credits cash.

The live observation now discovers OPEN, REDEEMABLE and REDEEMABLE_LOST token
holdings through the bounded Data API v2 cursor contract and checks discovered
quantities against native CLOB balances. Raw response bodies and SHA-256 checksums
are retained in the private observation. The request includes deposits/withdrawals
explicitly when reading activity since the previous observation. Ambiguous activity,
external trades, settlements/conversions and indexing gaps remain explicit. Known
indexed funding and unattributed rebates are disclosed separately; they are not
assigned to a trading strategy. This extends token discovery beyond application
orders but is still **venue-indexed, not an atomic chain-final wallet proof**.
The existing `cash_usdc` field is a compatibility name for native CLOB collateral
units; no USDC/pUSD conversion or redemption equivalence is asserted.

POST `/api/trades/analytics/reconciliation-reports` accepts only owned opening and
closing IDs and saves a content-addressed report. Repeated/concurrent identical
saves are idempotent. GET `/api/trades/analytics/reconciliation-reports/{id}` reads
the owned artifact and checks its content hash without recomputing current inputs.
The v0041 migration adds only `live_attribution_reports`; export saved artifacts
before downgrading that table. There is no update/delete route and no balance,
trade or reservation mutation. The screen can save and download this evidence.

Every report exposes machine-readable acceptance checks. A numerical mismatch
fails reconciliation; missing proofs remain incomplete. `verified_net_realized_pnl`
is withheld until all required evidence and validation gates pass. Diagnostic
results, indicative marks, modeled baselines and actual earnings are never combined.
Forecast/model/timing/sizing contributions remain unknown without their own source
lineage. Structured logs and counters record report status/reason codes, input hash
and source counts without wallet credentials or balances.

| Issue criterion | Implementation and verification | Live evidence status |
| --- | --- | --- |
| Exchange cash/positions reconcile within tolerance | Exact cash/share residuals; opening-lot quantity checks; PostgreSQL cross-window test | Incomplete until full chain-final activity/settlement and actual costs are supplied |
| No duplicate fills | Economic identity deduplication before ledger/benchmark allocation; conflicts reject | Covered for gateway-confirmed inputs |
| Confirmed vs estimated rebates | Separate fields; estimates excluded from cash/P&L; unallocated indexed rebates stay outside strategy totals | Actual per-fill native rebate proof remains unknown |
| Strategy totals add consistently | Entry-strategy FIFO allocation, prorated entry/exit costs; boundary and PostgreSQL tests | Proven only for rows with complete basis/cost inputs |
| Figures trace to fills/versioned benchmark | Intent/fill IDs, opening-lot lineage, native book/rule IDs, benchmark IDs, input hash, saved artifact | Missing archived point-in-time books explicitly abstain |
| Missing costs unknown | Nullable costs/net/marks, no fee-rate inference, verified net withheld | Covered |
| Forward baseline/release decision | Existing research/replay remains separate; no forward net-performance study is available | Incomplete; release abstains |

These gates deliberately do **not** mark issue #3 complete. Its empirical forward
validation cannot be manufactured from fixtures, and chain-final settlement/cost
provenance still coordinates with #24/#56. The numerical and failure-path tests
prove the implemented behavior, not actual user profitability or trading readiness.

Provider contracts checked November 21, 2026:
[Data API v2 migration](https://docs.polymarket.com/migrate/data-api-v1-to-v2),
[wallet activity](https://docs.polymarket.com/trading/wallet-activity), and
[fees](https://docs.polymarket.com/trading/fees). No TradingView inputs are used.
