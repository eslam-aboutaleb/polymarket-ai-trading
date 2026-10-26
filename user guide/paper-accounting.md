# Paper accounts and NAV (#27 / #35)

Paper accounts hold **modeled USDC and token shares**, separate from wallet funds.
No paper operation signs, submits, cancels, redeems, or transfers a live order.
Live release gates remain unchanged.

## Use a paper account

1. Select your account and paper mode. On Dashboard → Equity Curve, open **Paper account**.
2. Enter an explicit **fee assumption in basis points** and save it. Zero is a valid
   explicit assumption; a missing fee never silently becomes zero. This is a model
   input, not a verified fee charged by Polymarket.
3. For old simulated history, read and acknowledge **Exclude unverified historical
   results**. Old records remain readable; this starts separate accounting using
   the configured initial paper funding. It does not reconstruct historical cash.
4. Enter the native market condition ID and choose **Refresh public market evidence**
   to capture bounded public metadata and books. Automatic paper runners and the
   hourly writer also refresh public receipts when native capture is enabled.
5. Use **Record hourly paper NAV** to sample the account. The first sample in each
   UTC hour is immutable; another sample in that hour returns the existing point.
   Account controls show current balances, while the chart shows hourly samples.

Starting funding comes from `paper_balance` once. Editing that setting after the
journal starts cannot reset cash or replace NAV. Subsequent deposits/withdrawals
are explicit, idempotent paper cash-flow events, not profit. They do not move money.

## Accounting and fill policy

Each account has its own append-only economic journal, order results and hourly
NAV points. PostgreSQL account locks serialize strategies, initial funding,
configuration, settlement and snapshots. Journal events, modeled UserTrade rows,
and intent results commit or roll back together. Repeated intent keys replay the
original result; changing the economic request under that key is rejected.

`NAV = available cash + reserved cash + Σ(token shares × indicative exit bid)`.
Total cash already includes reservations: reserves are never counted twice.
Fees are charged once at the modeled fill and included in lot accounting. Realized
PnL comes from closes/final payouts; unrealized PnL uses remaining lot basis.

The implemented policy is `paper-account-IOC-v1`:

- Immediate-or-cancel only. Available native levels are walked with a limit,
  inventory/cash caps, captured tick/minimum size, and an explicit modeled lot
  precision (default 0.01 shares). Partial remainders are canceled and reported.
- Quote source time and receipt time must precede the decision and satisfy the
  freshness policy (default 60 seconds). Missing, corrupt, crossed, off-tick or
  mismatched evidence produces an unfilled result; it cannot manufacture trades.
- Latency defaults to an explicitly modeled zero. A positive latency assumption
  abstains because post-latency execution evidence is unavailable.
- Repeated snapshots do not prove liquidity replenishment. All strategies in an
  account share a cumulative token/side/price depth budget. Exhausted depth stays
  unavailable until a separately evidenced replenishment policy is implemented.
- Resting GTC and maker queue fills remain unsupported and unfilled. Existing maker
  quote intents remain readable; this does not complete #34's queue/flow lifecycle.
- Immutable result policy includes fee provenance, source receipt, source clock,
  decision time, rule fingerprint, size rules, model version and requested units.
  These are modeled executions, never exchange-confirmed fills.

| Runner | Input amount | Booking |
| --- | --- | --- |
| Copy | USDC budget | Shared journal; no simulated win-streak increment on entry |
| Latency | USDC budget | Native depth only; Binance prices are signals, not settlement evidence |
| Generic paper adapter | Shares (or explicit USDC metadata) | Requires a caller-owned database session and IOC order type |
| Stop-loss / take-profit | Shares | Close actual inventory; partial remainder stays active |
| Inverse | Sell shares, buy USDC budget | Second leg uses modeled sell proceeds or configured fixed amount; incomplete reversals require review |
| Maker | Quote intent | No fabricated maker fill without order-flow/queue evidence |
| Indicator backtests | Historical price model | Existing indicative/legacy labels retained; price-only results are not imported as journal fills |

## Marks, settlement, and history

Marks are **indicative best bids**, not a guaranteed liquidation value. An open
position without a fresh, matching mark makes NAV/unrealized PnL **unknown**. The
API returns null; the chart leaves gaps and the summary does not replace unknown
values with zero or the opening balance. Mark provenance retains capture ID,
source/receipt clocks and rule identity. No additional trade is needed for a new
mark to change the next hourly NAV sample.

Settlement requires paired, finalized native CLOB/Gamma binary payout evidence,
matching the held position's rule fingerprint and token/outcome mapping. Winner
and loser payouts are journal events, not real USDC redemption. Disputed, void,
reversed, changed-rule or unsupported payout evidence remains unresolved; no
external asset price is substituted for a venue outcome. If later captured final
evidence revises or disputes a booked payout, NAV becomes unknown and new fills
and paper withdrawals pause for reconciliation. Historical events are preserved;
a guessed reversal cannot manufacture a corrected balance.

Paper and live chart series are separate. An omitted mode selects live; it never
combines two bankrolls. Account headers are validated strictly on financial reads
and writes: an invalid, missing or foreign requested account cannot silently fall
back to personal. With no header, the owned personal account is used.

Old baseline-only snapshots are `legacy_unverified` with null NAV and an explicitly
named `legacy_reported_balance` for audit. Original UserTrades and old reports are
preserved. Live execution aggregates exclude all simulated PnL and fees; trade
rows disclose modeled or legacy execution quality. Daily paper digests use journal
NAV and paper positions, and skip delivery when the opening/current observations
are unknown or cash flows prevent an unqualified comparison.

Period change subtracts net contributions. Percentage returns and drawdown are
unavailable across unknown/legacy points, missing hourly observations or cash-flow
changes (even offsetting deposits and withdrawals). No Sharpe/annualized return is
invented from these observations.

## Operator rollout, audit and rollback

1. Back up the database, then run `alembic upgrade head` from `backend/`. Revision
   `20261026_0034_paper_accounts` adds four tables without rewriting financial history.
2. Set `PAPER_JOURNAL_V2_ENABLED=true` to enable modeled fills and NAV snapshots.
   Enable `NATIVE_CAPTURE_ENABLED=true` for bounded public refresh. Defaults are off.
   While the journal flag is off, new paper fills are paused and no replacement
   flat `paper_balance` snapshots are written.
3. Configure each account's fee assumption and acknowledge legacy exclusion when
   needed. Compare the named old baseline with the new journal epoch as separate
   models; do not interpret the difference as historical investment performance.
4. Inspect API `quality`, `missing_marks`, `mark_provenance`, `captured_at`, funding,
   cash and reservations. Order logs include account/status/reason; snapshot counters
   distinguish modeled, incomplete and legacy observations. Monitor snapshot age,
   missing marks, insufficient liquidity and unresolved settlements. Whole-account
   replay currently scales with journal history; benchmark larger accounts before
   increasing retention or refresh frequency. Public refresh is bounded to ten
   held markets per account/hourly pass and ten seconds per market.
5. Roll back by disabling the flag first. This preserves journal evidence and
   stops new economic mutations from strategy adapters and the snapshot worker.
   Database downgrade to `20261024_0033_live_orders` **drops the four new
   paper tables**; export/back them up before downgrading. Legacy tables remain.

A bounded metadata reprocessor defaults to dry-run (from `backend/`):

```bash
PYTHONPATH=.:../services/shared .venv/bin/python scripts/classify_paper_history.py --limit 500
# After reviewing the counts, label legacy metadata only:
PYTHONPATH=.:../services/shared .venv/bin/python scripts/classify_paper_history.py --apply --limit 500
```

Continue with `--after <next_after>` until `scanned` is zero. Classification preserves
amount, price, PnL and original metadata; it never fabricates fills, fees, balances
or migration-time returns. A marker alone cannot validate a trade: it must match
an owned durable order result and trade ID.

## Validation

- `pytest --no-cov tests/test_paper_accounting.py tests/test_paper_equity.py tests/test_paper_runners.py`
- `TEST_DATABASE_URL=<disposable-postgres-url> pytest --no-cov tests/integration/test_paper_accounts_postgres.py`
- From `frontend/`: `npm run test:paper` (real HTTP/SQL/accounting/chart, controlled public receipts).

PostgreSQL tests cover simultaneous retries, shared strategy depth, process exit
before commit, bootstrap upgrade and reversible schema changes. CI runs these with
its PostgreSQL service. Browser tests never use real wallet keys or permit private
order execution. They are separate from WalletConnect pairing tests.

## Acceptance evidence

The following maps the acceptance checklists in #27 and #35 to executable
regressions. Test paths below are relative to `backend/tests/` unless noted.

| Issue criterion | Evidence |
| --- | --- |
| #27 identical cash/share accounting across runners | `test_paper_runners.py::test_identical_native_events_book_identical_economics_without_signing` compares copy, latency and generic adapters on matched native receipts. |
| #27 partial/cancel/retry, depth, stale book, fees, unresolved settlement | `test_paper_ledger.py` exercises reservations, partial fills, cancellation, duplicate events and conservation; `test_paper_accounting.py` covers shared depth, missing fees, stale evidence, disputed and revised payouts. |
| #27 separate live fills, modeled fills and indicative marks | `test_paper_runners.py::test_paper_realized_profits_never_enter_live_execution_metrics`, account API tests and browser NAV tests verify separate aggregates and modeled/unknown labels. |
| #27 no signer/post access | Runner regressions patch both SDKs, wallet credential loading and the live gateway; the browser backend rejects both SDKs' create/post calls. |
| #27 flags and legacy compatibility | `test_feature_disabled_never_writes_economic_events` and `test_legacy_reprocessor_preserves_numbers_and_requires_exclusion_epoch` in `test_paper_accounting.py`; legacy snapshots remain explicitly unverified. |
| #35 1,000 funding, 25 shares at 0.40, 0.60 mark gives 1,005 NAV | `test_paper_accounting.py::test_mark_change_without_trade_and_partial_exit_reconcile`, API regression and `frontend/e2e/paper/nav.spec.ts`. |
| #35 close 10 shares at 0.70 | The same golden regression asserts 997 cash, 3 realized PnL and 1,007.50 NAV; browser tests verify partial close and funding separation. |
| #35 fees, partials, reservations, insufficient cash, inverse, settlement, retries | Ledger/accounting suites, inverse two-leg runner test, and PostgreSQL concurrent retry/shared-depth regressions. |
| #35 funding is not running NAV; empty account funding | `test_snapshot_api_returns_the_persisted_hour_and_never_resets_funding` and account-isolation API regression in `test_paper_equity.py`. |
| #35 missing/stale mark is unknown | Accounting missing-mark regression and browser mark-movement/stale-gap scenario. |
| #35 mode/account/time isolation | `test_null_gaps_cashflows_and_mode_never_mix`, `test_asof_window_excludes_later_receipts_and_normalizes_timezone`, strict account API checks and delayed-response browser regression. |
| #35 API, migration, history, chart, scheduled writer | `test_paper_equity.py`, `integration/test_paper_accounts_postgres.py`, legacy classifier regression and all four paper browser scenarios. |
| #35 live gates preserved | Private execution fences above; existing trading-safety CI runs unchanged. No live release policy is relaxed. |

The maker acceptance is **safe abstention without queue evidence**, not a claim
that partial maker executions have been reconstructed. The common ledger tests
partial fill/cancel economics independently of strategy; production maker quotes
remain unfilled. Queue-driven maker execution belongs to #34. Likewise, #25 owns
indicator backtest execution changes and consumes this accounting contract.

Security regression `test_paper_conflicts_hide_internal_errors_and_rollback` covers
all four mutating financial API endpoints: exception details never reach clients
and a flushed mutation is rolled back before returning a fixed conflict response.
