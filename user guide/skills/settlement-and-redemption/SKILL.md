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
receipt evidence returns null, never invented zero. `available_cash_usdc` and
`realized_pnl_usdc` remain null with `accounting_status=abstain`: current records do
not prove complete share/fee reconciliation or native-gas conversion to USDC. Use the
separately reconciled ledger for these totals; an observed payout is not spendable
account cash. This analytical PR does not implement #56's durable submission intents.

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
