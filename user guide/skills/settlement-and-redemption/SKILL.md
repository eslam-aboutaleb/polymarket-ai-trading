# Settlement and redemption analytics v1

This read-only implemented detector supports issue #24. It never signs, broadcasts,
retries redemption transactions, changes an accounting balance, or changes trading
policy. The existing redemption service and execution barriers remain authoritative.

## Inputs and interpretation

`POST /api/settlement/analyze` evaluates **declared hypothetical scenarios**. Its
result always says `provenance_class=declared_hypothesis`, `source_verified=false`,
`execution_eligible=false`. A caller's claimed receipt is a scenario input, never
proof of chain finality. Decimal values are finite, at most 1e18 with at most 18
fractional places. Timestamps require offsets; internal calculations use precision
80 independently of the caller's Decimal context. Attempts are bounded to 100.

Supply market/state, shares, entry cost, locked collateral, starting **already
available** cash, payout per share, dispute deadline and attempts. Funding rate,
expected/stressed duration and gas costs are explicit scenario assumptions, with
8% / 2 days / 14 days defaults; these are not measured empirical distributions.
`redeemed_shares` permits partial claims. An omitted redeemed quantity explicitly
models a full-position claim; multiple such claims fail the over-redemption check.

A 40-share claim from 100 shares costing 60 USDC at payout 1 credits **40 cash**,
releases 24 of cost basis, retains 36 locked cost basis and recognizes 16 scenario
profit before gas. Remaining balances refer to 60 unredeemed shares. Distinct partial
claims may complete the position; duplicate identities/hashes, excess shares,
payout mismatches, open disputes, reversals and future receipts abstain and release
no hypothetical cash. Valid proxy plus EOA balances may split the remaining shares.
Every discrepancy code is logged by the API for operator review.

## Recorded account read-side

`GET /api/settlement/recorded` requires authentication and strict `X-Account-Id`
ownership (foreign/unknown IDs return 403). It extends the existing
`position_lifecycle_service.py` and `redemption_service.py` read interfaces. It reads
up to 500 application-recorded trades/reservations and 20 redemption attempts;
missing fills, stale rules, mutable future state and unsupported lifecycle states
are explicit unknowns. Legacy/null-account attempts are never attributed to another
account. The response is not a complete external wallet reconciliation.

Preview records have no transaction hash and no broadcast. Other recorded statuses
are not chain evidence: a fixed Polygon public RPC is queried for chain ID 137,
receipt, canonical block and current head. A supported receipt requires a successful
transaction, matching hash/block, 128 confirmations, the canonical CTF address,
root `PayoutRedemption` condition/index-set/recipient identity and legacy USDC.e
collateral. Duplicate transaction hashes are not added twice. Re-read after a reorg
returns uncertainty; DB statuses are not rewritten by the analytical read.

This first adapter supports root binary USDC.e payouts to the owner's EOA. Proxy,
nested positions, new collateral/protocols, mixed-account/external activity and
missing/malformed/unavailable RPC receipts abstain. A provider RPC observation is
not a cryptographic consensus proof. Each accepted observation has an evidence hash,
block identity and confirmation count. RPC is fixed, requests use 2-second timeouts,
the whole receipt pass has an 8-second deadline, and responses are bounded to 512 KiB.
No user URL, wallet key or caller-supplied receipt enters this path.

`observed_redemption_cashflows_usdc` sums only supported observed events. Missing
receipt evidence returns null, never invented zero.

### Accounting reconciliation (`accounting_status`)

`realized_pnl_usdc` and `available_cash_usdc` are filled only when every input agrees,
otherwise they stay null with `accounting_status=abstain` and per-scope reason codes
(`accounting_reasons`, `conditions[].reasons`):

- Realized P&L is recognized per redeemed condition only when recorded inventory
  (shares times entry price plus recorded fee) reconciles exactly (to 1e-6 USDC) with
  the canonical receipt payout **and** the on-chain CTF payout vector
  (`payoutNumerators`/`payoutDenominator`, read-only `eth_call`), the redeemed index
  sets cover the recorded tokens, there is no pending order on the condition and at
  most one non-zero redemption exists. Zero-payout redemptions of losing tokens
  recognize the loss. Partial, duplicate, unrecorded or disagreeing redemptions
  abstain for manual review. Native gas is reported in `gas_fee_native` and is **not**
  converted to USDC, so P&L is explicitly `realized_pnl_excludes_native_gas`.
- `available_cash_usdc` is the latest integrity-checked balance snapshot cash minus
  open-order reservations. It abstains if the snapshot is older than 15 minutes,
  predates a redemption block, saw active/unsettled orders, disagrees with recorded
  wallet inventory, or reservations exceed cash. Unredeemed and unsettled positions are
  reported in `locked_collateral_usdc` and never counted as cash.
- `shadow_comparison` compares the existing bot's recorded sell-side realized P&L with
  the settlement-reconciled redemption P&L for that account (recorded, not assumed).

### Persisted lifecycle (`settlement_transitions`)

`POST /api/settlement/sync` appends server-derived transitions to an append-only,
hash-chained, per account/condition/token ledger: `FINAL` from the CTF payout vector,
then `TX_CONFIRMED` (canonical receipt) / `TX_PENDING` / `TX_FAILED`. The previous
state is read from the ledger, never from the caller. `POST /api/settlement/transitions`
accepts only `venue_reported` or `operator_asserted` observations (for example UMA
proposal/dispute stages, which the Polygon CTF does not expose) and can never claim
chain-verified or transaction states. `GET /api/settlement/transitions?condition_id=`
returns history plus a hash-chain integrity verdict. Database triggers reject UPDATE and
DELETE. Every discrepancy code is also written once to `app_events`
(`settlement_discrepancy_flagged`) for manual review. Migration
`20261125_0060_settlement_ledger` refuses a destructive downgrade once evidence exists.

Limits: UMA proposal/dispute stages are not derivable on-chain here; the YES/NO to CTF
index mapping is verified only through the payout cross-check; native gas has no USDC
price source; proxy/Safe wallets and non-USDC.e collateral abstain; the public RPC
canary returns HTTP 403 in the development environment, so live receipt verification is
exercised through a deterministic JSON-RPC transport, not a live node. This issue does
not implement #56's durable submission intents.

## Agent integration, telemetry and rollback

Authenticated backend routes and Research MCP tools are disabled by default via
`settlement_redemption_enabled=false`. The generated JSON request schema is exact.
`settlement_and_redemption` and `recorded_settlement_and_redemption` are fixed,
versioned tools discoverable from `ResearchMCPClient` by Research, Analysis and the
orchestrator; no money-path tool is added. The common MCP bridge enforces a 10-second
HTTP timeout, response size, known schema version and `execution_eligible=false`.

Monitor `settlement_discrepancy` / `settlement_scenario_discrepancy` codes, RPC error
rate, stale rule coverage and unknown accounting. Turning the feature flag off rolls
back exposure without changing financial history. No migration is needed: the
service does not write any new model or mutate existing tables. A future shared SDK
adapter must keep `settlement-lifecycle-v1`, exact Decimal strings, explicit source
classes and unknowns; venue and account/RPC code stays in this application.

## Evidence and verification

Run `scripts/settlement-shadow.py --output /tmp/settlement-shadow.json` in the frozen
backend environment. The committed report is reproduced from the hashed public
quote/label dataset. Its five matched cases compare an unsafe end-date cash-release
assumption against receipt-required release, and show hypothetical funding/gas
sensitivity. Observed label-receipt delay is not actual chain-finality time or a
measured capital-lockup distribution. No actual fills/P&L or prior bot decisions
are invented, and this diagnostic was not a precommitted strategy hypothesis.

Tests cover Decimal context independence, partial/duplicate/late claims, payout
revisions, splits between proxy/EOA balances, dispute windows, source identity,
canonical-block replacement, failed/missing receipts, wrong collateral/wallet/chain,
RPC outages, account isolation, repeated reads and absence of SQL writes. PostgreSQL
integration and offline replay are required in CI. The environment's public RPC
canary returned HTTP 403; controlled HTTP transport validates success and fault
branches without claiming a successful live-chain run.

Initial evidence status is **implemented detector**, not historically validated or
production approved. Independent replication checklist: frozen install; reproduce
schema and public dataset hashes; run receipt negative controls; run disposable
PostgreSQL account tests; verify feature disabled and signing/submission untouched;
review unavailable historical account and chain evidence before any promotion.

References: [CTF event source](https://github.com/gnosis/conditional-tokens-contracts/blob/master/contracts/ConditionalTokens.sol),
[CTF developer guide](https://gnosis-conditional-tokens.readthedocs.io/en/stable/developer-guide.html).
