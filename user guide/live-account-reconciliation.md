# Live accounting evidence — issue #3, first increment

This extends the existing execution analytics, FIFO reconciler, `UserTrade`, and
Execution Analytics screen. It adds observations and a diagnostic report, not a
second ledger or an order submission path. Issue #3 remains open.

## Behavior and evidence

The existing FIFO projection now consumes earlier closes on every replay and
separates accounts and outcome token IDs. Existing stored P&L is not rewritten.
Rows without token IDs stay in a separate legacy market bucket. Monetary matching
uses the existing fixed Decimal accounting context; legacy persisted floats remain
legacy projections. Analytics with missing or invalid fees withhold total fees,
net P&L, fee drag and Edge Score; known fees are still exposed separately. The
fill monitor preserves unknown fees rather than inventing zero.

With `LIVE_ACCOUNT_RECONCILIATION_ENABLED=true`, Execution Analytics offers:

- `POST /api/trades/analytics/balance-snapshots`: authenticated native CLOB cash
  and known-token balance observations, using existing stored credentials.
- `GET /api/trades/analytics/balance-snapshots`: the latest 20 owned observations.
- `GET /api/trades/analytics/reconciliation?opening_id=…&closing_id=…`: a read-only
  report and downloadable structured evidence between two observations.

All endpoints resolve and authorize `X-Account-Id`. The report verifies stored
observation content hashes, references gateway-confirmed fill audit events and
preserves a versioned input hash. Execution timestamps are added to new audit
records without changing the immutable fill dictionaries used by execution
recovery. Older fills without timestamps are explicitly unsupported, not
backfilled. Duplicate fill identities count once; conflicting contents fail.

FIFO realized gross P&L is allocated to the entry strategy. Entry and exit costs
are allocated pro rata to consumed shares. Strategy realized totals are additive.
Estimated rebates are never cash or realized profit. Actual fees and confirmed
rebates require explicit evidence; `fee_rate_bps` is not treated as an actual fee.
Native gateway receipts currently do not provide these cost proofs, so native net
P&L normally remains unknown. No benchmark profitability claim is made.

Cash residual = closing cash − opening cash − confirmed net trading cashflow.
Token residual = closing quantity − opening quantity − signed gross fills.
Tolerances are 0.000001 USDC and 0.000001 shares. Unknown cost/basis or skipped
fill evidence withholds the corresponding financial result. Deposits, withdrawals,
settlements, token-denominated fees and unexplained changes are not attributed to
profit. Unrealized marks and spendable cash remain unknown.

## Deliberate limits and remaining acceptance work

Actual observations cover **known application tokens only**; they always produce
`full_wallet_reconciled=false` and cannot authorize execution. The native read is
not a chain-finalized atomic wallet snapshot. Cash is sampled before and after
token reads, and the application's event watermark must stay unchanged. This
rejects some races but cannot prove the absence of external activity.

Only the personal account is supported for balance capture. Logical subaccounts,
or wallets with other-account live order history, are rejected because native
wallet cash cannot be independently assigned to them. Legacy recorded analytics
remain account scoped and available for subaccounts. GET analytics are excluded
from the service worker's URL-only offline cache to avoid account-header reuse.

Capture has a 2/minute rate limit, 500-order, 20-token and 10-second elapsed bounds
(with transport calls subject to their SDK timeout). Reports allow at most 500
fills and 500 orders; observations list at most 20 records. Bounds return errors
rather than silently truncating financial inputs. There is no arbitrary endpoint,
credential derivation, signing, submit or cancel operation in capture.

Before closing #3, add complete external wallet activity ingestion and chain
settlement/reorg evidence (coordinate #24/#56), proven actual fee assets and
rebates, opening-lot lineage across windows, verified marks and benchmark snapshots,
implementation-shortfall decomposition, and a disjoint-event baseline with
uncertainty/release thresholds. This change does not meet those remaining criteria
and does not certify the application for real-money use.

## Verification and rollback

Tests cover repeated FIFO replay, token/account separation, duplicate/conflicting
receipts, Decimal context independence, timezone ordering, unknown costs/basis,
rebate separation and residuals. Real API/SQL tests verify owner isolation,
read-only GETs, hash tampering, unavailable timestamps, feature disable, changing
cash and read-only native transport. PostgreSQL tests exercise the additive
migration, exact evidence round trip and rollback without changing users/accounts.
The Playwright flow uses the real API/PostgreSQL and downloads unknown-cost evidence;
only the unrelated application shell endpoints are stubbed. No funded orders run.

Apply Alembic head before enabling the diagnostic flag. The migration handles the
historical current-model bootstrap and adds no financial-history backfill. Default
flag is false. Disable the flag to hide the diagnostic panel; preserve observations
for audit. Downgrade deletes only the new observation table and must be preceded
by an evidence export if those records must be retained. Trading-enabled settings
and the user's paper/live selection are unchanged.
