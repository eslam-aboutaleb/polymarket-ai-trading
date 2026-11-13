# Historical provider contract and acceptance (#54)

The official CLOB `/prices-history` canary is opt-in
(`HISTORICAL_FEED_CANARY_ENABLED=false` by default), public and credential-free.
It has a process-local 12 requests/minute limit, a 15-second overall deadline,
a 4 MB response limit, and a 1–168 hour requested window. Multiple workers
share the venue's normal infrastructure limits; the local canary limit is
not represented as a distributed quota.

The only approved provider contract is `polymarket_clob_prices_history`,
normalization version `prices_history_feed_v1`. Its permitted use is
`research_indicative_only`; it does not authorize order execution or establish
oracle settlement equivalence. A provider-name substitution or modified
contract revision fails validation rather than changing sources silently.

Coverage counts distinct requested hourly slots, not rows. Dense bursts,
duplicate/out-of-order samples, future/out-of-window timestamps, missing hours,
partial pages, stale history, invalid prices and malformed payloads fail closed.
Hourly publication jitter has a conservative 60-second lower cadence tolerance;
this is explicit normalization policy, not a claim of regular executable depth.
Incomplete coverage is never READY, even when over 80% of the samples exist.
HTTP 401/403, 429 and 5xx have separate unauthorized, rate-limited and unavailable
outcomes. No empty series becomes a successful backtest.

`GET /api/market-data/provider-contract` exposes the contract and opt-in state.
`POST /api/market-data/provider-canary?token_id=...` captures a report and its raw
public response in the existing immutable venue-observation archive. The report
returns the archive ID, source token, actual/requested clocks, raw SHA-256,
coverage, missing bars, delay and readiness separately. No schema migration is
needed: the archive already preserves typed public payloads and first receipts.
The backtesting page's Historical feed provider panel presents these fields and
clears stale successful output on provider errors or a disabled canary.

## Acceptance evidence

- `backend/tests/test_historical_provider_contract.py`: recorded 200/401/403/
  429/5xx, malformed/empty/truncated/duplicate/out-of-order/unreconciled series,
  source substitutions, exact request-window coverage and local rate limiting.
  The existing recorded official CLOB history fixture retains real missing
  hours; it is not rewritten into a complete series.
- `backend/tests/integration/test_provider_contract_postgres.py`: real
  PostgreSQL retains pre-contract archived observations, precise prices and
  successful/error/partial provider reports across idempotent schema upgrade;
  raw response hashes and capture idempotency are verified after commit.
- `npm run test:provider`: nine real browser/API/PostgreSQL scenarios cover
  successful history, missing hours, empty/malformed data, 401/403/429/503 and
  a canary disabled after an earlier success. Only the outbound public HTTP
  boundary is controlled; the application UI, routes, parsing and database run.
- `historical-provider-live-canary.json`: live, public, credential-free Gamma
  market selection and CLOB price-history probe, with raw bytes preserved as
  base64 and a SHA-256 checksum. Its observed status and capture time are
  evidence for that request, not a continuing availability guarantee.
- Required CI runs the provider unit tests, PostgreSQL fixture and browser
  suite. Existing live-trading policy and signing/submission paths are unchanged.

## Reproduction

```sh
uv run --project backend --frozen pytest --no-cov backend/tests/test_historical_provider_contract.py
TEST_DATABASE_URL=postgresql:///disposable_db uv run --project backend --frozen pytest --no-cov backend/tests/integration/test_provider_contract_postgres.py
cd frontend
TEST_DATABASE_URL=postgresql:///disposable_db npm run test:provider
```

The PostgreSQL fixture and browser harness create and remove isolated random
schemas. They never seed or delete application accounts, credentials or trades.
Rollback is disabling the feature flag; retained public receipts stay readable.

Reference: [official CLOB price history contract](https://docs.polymarket.com/api-reference/markets/get-prices-history).
