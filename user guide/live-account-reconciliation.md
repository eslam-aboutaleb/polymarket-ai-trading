# Live accounting evidence — issue #3

The Execution Analytics screen captures read-only balance observations, verifies
native trading receipts, and saves immutable attribution reports. Trading-enabled
settings and the user's paper/live selection remain unchanged. Reports never
submit, cancel, sign, redeem, or authorize an order.

## Enablement and supported scope

Apply Alembic head (additive migrations v0040–v0042) before enabling
`LIVE_ACCOUNT_RECONCILIATION_ENABLED`, which defaults to false. Disable the flag to
hide diagnostics while retaining observations. Export evidence before downgrading
the observation, report, or study tables. No financial-history backfill occurs.

All routes authorize the owner and account. Shared-wallet/subaccount attribution
is rejected without allocation evidence. Analytics bypasses the offline URL-only
cache; account changes clear results. Snapshot and report IDs are content hashes;
GET validates stored evidence without fetching current RPC data. Identical report
saves are conflict-safe and idempotent. No update/delete endpoint exists.

The verified asset scope is Polygon 137 native pUSD collateral and the configured
Conditional Tokens contract, with the two V2 exchanges checked against their
on-chain collateral/CTF getters. `cash_usdc` remains a compatibility field name;
it does not assert USDC/pUSD parity. Other assets, gas, relayer costs, model/service
expenses and external model weights are outside this proof. The report explicitly
keeps `full_wallet_inventory_verified=false`; supported-scope evidence must not be
presented as total wallet profit.

## Capture, verify, and continue a window

1. Capture a finalized opening snapshot. The configured Polygon RPC is used only
   for allowlisted read methods. EIP-1898 calls pin balances to a canonical finalized
   block hash. Native venue responses remain separate evidence.
2. Keep native book/rule observations from before each decision. The
   `arrival-midpoint-spread-v1` comparison requires both source and receipt clocks
   before the decision, within 60 seconds. Missing/stale/future books stay unknown.
3. Capture a finalized closing snapshot, then select **Verify finalized evidence
   and save**. The verifier scans collateral and CTF transfers over the entire
   block interval and checks exact bookend balance identities.
4. For the next window, select the prior saved report as opening basis. Its closing
   snapshot must equal the new opening. Remaining FIFO lots, entry fees, token
   condition lineage and verified gateway identities carry forward through the
   owned, hash-checked artifact. A new chain study can start without prior basis
   only when opening token holdings are zero. Historical native-only diagnostics
   can reconstruct bounded gateway history, but cannot assert chain provenance.
5. Save/download each report and preserve the linked observations. Capture at
   least every 12 hours; study policy rejects excessive observation gaps.

Capture bounds: 2/minute, 500 active application orders, 20 combined known/discovered
tokens; native discovery has 10 pages, 500 rows/stream and 256 KiB/response. Report
windows cap at 5,000 audit records and 500 referenced orders. Chain verification
caps at 50,000 blocks, 50 order/transaction groups, 200 RPC calls, 1 MiB/response,
8 MiB total and a 30-second deadline. Log pages with 1,000 entries are rejected as
potentially truncated. Bounds reject incomplete coverage; use shorter windows.

Actual V2 `OrderFilled` receipt fees replace unknown gateway fees only after
order hash, wallet, token, side, size, principal, successful receipt, canonical block
and unique log identity agree. Native match IDs are aggregated by order/transaction;
no unsupported per-match fee split is invented. Settlement uses finalized execution
order, separately retaining the venue match clock. Receipt replay cannot double
count fills or consume an opening lot twice.

Binary CTF redemptions require the payout event, condition-derived token IDs,
winning/losing payout vector, exact burn quantities and actual collateral credit.
Direct CTF and the known pUSD redemption adapters are supported. Ambiguous/nested
collections, conversions or unknown adapters abstain. Venue-final metadata alone
never credits realized cash.

Cash/share residual tolerance is 0.000001 native units. Classified native activity
must match the exact chain transfer to identify deposits, withdrawals or rebates.
Funding is excluded from profit. Confirmed unallocated rebates have their own
strategy bucket; estimates never become cash. Unknown collateral or token movements
keep the report incomplete, including unexplained token roundtrips. Entry and exit
fees allocate once through FIFO; strategy totals must sum to the reported net.

Every figure retains fill/intent, receipt/log, rule/book or versioned comparator
references. Spread plus remaining execution difference equals implementation
shortfall; it is not an additional P&L deduction or proof of causal impact.
Indicative bid marks remain distinct from realized cash. Missing basis or costs
withhold net P&L and release evidence rather than substituting zero.

## Prospective forward study

Register a policy **before** collecting the selected strategy's future decisions.
Registration freezes source/configuration fingerprints, nonsecret configuration,
development-event exclusions, enrollment/settlement deadlines, sample requirement,
capital limit, observation-gap limit and drawdown bound. This is the versioned
`prospective-paired-event-study-v2` policy. External models/services remain unattested.

Every eligible future BUY intent is included. Filled entries need actual closed
FIFO outcomes and receipt costs. Terminal zero-fill decisions remain in the paired
control comparison; they cannot supply the minimum 20 **executed event clusters**.
Repeated trades in one event count as one cluster. Missing terminal evidence,
changed implementation/configuration, prior-event overlap, unavailable native
controls, missing costs or missing observations keep evaluation incomplete.

The control reuses PaperLedger and archived native depth/fee/payout rules for a
same-time, same-capital YES buy-and-hold baseline. It is modeled counterfactual
performance, labeled separately from actual outcomes. Each observation carries
source references and consumes archived liquidity once. Linked reports supply the
complete study interval. Cashflow-adjusted indicative scoped NAV supplies observed
point drawdown, which is a lower bound between observations, not full wallet NAV.

The evaluator reports event counts, seeded event-bootstrap 95% intervals and
assumptions. These are descriptive, assume comparable independent event clusters,
and do not correct for selecting among multiple studies. A complete positive
interval, positive actual net and acceptable drawdown allow only
`SHADOW_EVIDENCE_SUPPORTS_SCOPED_REVIEW`. Ties/negative evidence reject; missing data
abstains. No result automatically enables trading or claims proven profitability.

The test account can exercise paper/shadow collection without funded orders.
Synthetic acceptance fixtures prove behavior, not actual future results. Actual
forward acceptance requires the selected running account, archived decisions/books,
complete finalized observations and the future settlement deadline to elapse.
Issue #3 must remain open until that empirical evidence exists.

## Acceptance and verification

| Criterion | Implemented evidence | Missing-input behavior |
| --- | --- | --- |
| Cash and positions reconcile | Finalized scoped balances, complete bounded transfers, receipts and exact residuals | Incomplete/fail; never an invented residual |
| No duplicate fills | Economic deduplication, unique logs, immutable opening FIFO handoff | Conflicts reject |
| Rebates separated | Confirmed chain/activity credits, separate estimates and unallocated strategy | Unknown credits abstain |
| Strategy totals additive | Exact FIFO cost allocation, funding exclusion, rebate bucket | Net withheld if costs/basis missing |
| Figure lineage | Fill/intent and chain references; versioned native comparisons | Missing benchmark incomplete |
| Missing costs unknown | Nullable actual costs/net, no inferred fee-rate expense | Net/release withheld |
| Forward validation | Preregistered event-disjoint paired controls, counts/interval/drawdown | Incomplete until real observations mature |

The positive synthetic acceptance case passes every machine-readable gate, in both
SQLite and PostgreSQL. Negative cases cover reorgs, wrong chain/wallet/exchange,
missing transfers, duplicate logs, unsupported costs/settlements, event overlap,
changed implementation and incomplete studies. Browser tests use a real API and
PostgreSQL with synthetic read-only RPC receipts. They are not funded performance.

Verify a downloaded report or snapshot offline:

```sh
python3 scripts/verify-attribution-artifact.py downloaded-report.json
```

The ID covers canonical compact sorted-key JSON without its `report_id`/`id` field,
not the formatting of the download. Monetary/configuration decimals and timestamp
strings preserve their hashed representation. The verifier requires no credentials.

The [public contract canary](attribution-contract-canary-2026-11-22.json), collected
November 22, 2026, verifies the finalized chain, collateral decimals and exchange
asset getters without a wallet or order call. Reproduce it with:

```sh
backend/.venv/bin/python scripts/verify-attribution-contracts.py --output /tmp/attribution-contract-canary.json
```

Receipt ABI source is pinned to
[ctf-exchange-v2 ccc0596](https://github.com/Polymarket/ctf-exchange-v2/tree/ccc0596074f4dfd62c944fbca4de252893b82b4b).
Native provider references: [Data API migration](https://docs.polymarket.com/migrate/data-api-v1-to-v2),
[wallet activity](https://docs.polymarket.com/trading/wallet-activity), and
[fees](https://docs.polymarket.com/trading/fees). No TradingView input is used.
