# Capital-protection remediation: Stage 0

This change implements the execution freeze and durable halt foundation from
the October 20, 2026 specification. It does **not** establish production
readiness or complete the 16-issue roadmap. Live order submission remains
unavailable, including live conditional exits and emergency position sells.
Paper strategy calculations remain available.

## Implemented behavior

- Four server flags default to false: `LIVE_TRADING_ENABLED`,
  `LIVE_MARKET_MAKING_ENABLED`, `LIVE_COPY_TRADING_ENABLED`, and
  `LIVE_LATENCY_ARB_ENABLED`.
- Manual/external requests require the explicit `execution_mode: "live"`
  field. Missing, boolean, paper, or ambiguous modes cannot authorize live
  execution through these endpoints. Paper execution uses its separate engine.
- Manual and external routes refuse requests before loading credentials.
  Shared copy/manual/inverse/conditional-exit submission, direct maker
  submission, direct latency submission, and registered live engines are
  blocked before constructing an order signer/client.
- Enabling all flags still returns `DURABLE_EXECUTION_GATEWAY_REQUIRED`.
  Configuration cannot authorize the existing undurable paths. A sell side
  alone does not establish safe inventory reduction.
- `trading_control_states` stores global/wallet `RUNNING`, `HALTING`, `HALTED`,
  or `RECOVERY_REQUIRED` state, reason, timestamp and monotonic version. The
  migration seeds the global state as `RECOVERY_REQUIRED`. Missing state is
  unverified; it never grants permission.
- An emergency request atomically halts the wallet, disables copy/maker/latency/inverse
  automation and queues a durable operation. Wallet scope covers all existing
  sub-accounts because credentials are currently wallet scoped. Existing
  conditional triggers and positions are preserved.
- The cancellation worker claims committed operations using PostgreSQL
  `FOR UPDATE SKIP LOCKED`. Discovered order IDs commit before cancellation;
  recovery queries those identities even when the open-order listing is empty.
  A crash rolls back the cancellation phase, preserving the snapshot. Repeated
  cancellation is idempotent and cannot submit an order. The captured
  control version prevents stale work from overwriting a newer recovery request.
- Halt operations preserve both the target wallet owner and the initiating user,
  including when an administrator halts another wallet.
- Cancellation uses the existing authenticated proxy-wallet query path; a
  failed wallet-identity lookup, credential load, or exchange query remains
  explicitly unresolved. No EOA/proxy identity is guessed.
- The installed CLOB SDK drains all open-order pages. Cancellation evidence
  requires a matching order ID and explicit `CANCELED`/`CANCELLED` status.
  Disappearance, cancellation acknowledgment, and a concurrent fill do not
  imply cancellation or position closure. Partial-fill accounting still needs
  the future ledger/reconciler, so the recovery barrier remains in place.
- No automatic sell or unbounded price-zero liquidation occurs. Position counts
  stay null when authoritative reconciliation is unavailable; confirmed closes
  stay zero. The legacy portfolio fallback is not used as closure evidence.
- Recovery requests preserve the halt and never restart strategies. The legacy
  resume endpoint returns 409.
- Dashboard/Ops Center poll persisted operation results, including after reload,
  and show requested, confirmed, open, and unverified outcomes separately.

## API contracts

All routes require the existing authenticated session. Wallet operation reports
are owner-only; a foreign or unknown operation returns 404. Global halt requires
the actor's current database `is_admin` permission and does not confer access to
another wallet's operation reports or keys.

| Request | Result |
| --- | --- |
| `POST /api/v1/trading/emergency-stop?close_positions=false` | 202 after durable halt/outbox commit; returns operation ID |
| `GET /api/v1/trading/emergency-stop/{operation_id}` | Persisted evidence and unresolved exposure |
| `GET /api/v1/trading/emergency-stop` | Latest own operation, or null |
| `GET /api/v1/risk/summary` | Global/wallet safety state; financial state explicitly unverified |
| `POST /api/v1/risk/halt` with `{"scope":"user"}` | Queue own wallet halt |
| `POST /api/v1/risk/halt` with `{"scope":"global"}` | Admin-only global halt and cancellation operations for existing wallets |
| `POST /api/v1/risk/recovery-request` with `{"reason":"..."}` | Record request; `resumed: false` |

The existing `/api/trades/emergency-stop` POST/GET/status paths are aliases.
`close_positions=true` records the requested action but cannot authorize an exit.
The report explicitly includes `POSITION_EXIT_REQUIRES_DURABLE_GATEWAY` when the
worker processes that request. It must never be presented as a completed exit.

## Rollout and recovery

1. Keep live flags false. Deploy to a controlled environment first; no production
   migration or exchange action was performed by this code change.
2. Run `uv run --frozen alembic upgrade head` from `backend/`. The migration is
   additive. The Alembic bookkeeping column is widened on PostgreSQL to support
   existing descriptive revision IDs longer than its default 32 characters.
3. Run web and worker with the existing process roles. The halt queue drains only
   in `ROLE=worker`/`all`; without a worker its status remains `PENDING`.
4. Request the necessary wallet/global halts. The migration seeds the release
   barrier; it does not silently submit cancellation requests for every wallet.
5. Inspect operation reports. A failed query is unknown exposure. Cancellation
   attempts are not position-reduction or settlement evidence. Failed operations
   remain visible as `RECOVERY_REQUIRED`; a new halt request queues another
   cancellation attempt.
6. Reconcile positions and any external orders using authoritative venue records.
   Until controlled exits exist, exposed positions need management through the
   venue's own interface. The app cannot promise an exit price or fill.
7. Do not roll application code back to a release that permits legacy submission.
   Database downgrade refuses to delete modified halt state or operation history.
   The empty-development-database downgrade is tested. Never delete safety history
   to force a production downgrade.

Cancellation currently retains a database row lock across exchange I/O. This is
appropriate for cancellation-only Stage 0 work; it is not the submission-worker
lease/fencing design required by P2. Production SDK authentication and custody
still use the existing backend boundary, pending P3.

## Verification

CI includes mandatory freeze/fault tests, real PostgreSQL concurrency/crash tests,
PostgreSQL migration upgrade/downgrade/upgrade, and a deterministic browser check
of emergency-state reporting. These checks are scoped to Stage 0 and do not
substitute for the P5 financial, signer, Redis, venue, or independent security gates.

### Shared CI inheritance

The caller in `.github/workflows/ci.yml` pins each inherited workflow to the
reviewed `eslam-aboutaleb/ci-workflows` commit
`f989c2d0742cdab6cdef8437685d128b28904abc`:

| Shared workflow | Repository contract |
| --- | --- |
| `python.yml` | Locked backend Ruff/pytest, application-only coverage >= 80%, JUnit/coverage artifacts |
| `integration.yml` | `backend/Makefile`: frozen install, migration round trip, mandatory cancellation/security/PostgreSQL tests; runner provisions PostgreSQL and Redis |
| `browser.yml` | `frontend` npm lockfile and `test:safety` script; Chromium and failure artifacts |
| `workflow-lint.yml` | Actionlint against this repository's workflow definitions |
| `security.yml` | CodeQL for Python, JavaScript/TypeScript and Actions; high-severity new dependency review on PRs |

Local jobs retain repository-wide lint/duplication and the frontend build and
Prettier checks. The shared Node workflow requires unit-test scripts that this
frontend does not provide, so its existing build contract is retained. Existing
frontend dependency advisories remain visible in the non-blocking audit; the
new dependency-review job blocks newly introduced high-severity dependencies.
CodeQL uploads results; repository code-scanning rules determine which existing
findings block a merge.

The final `Quality gate` requires every selected job to succeed, including on
merge-queue runs. Pull requests, pushes to `main`, manual runs and a weekly
schedule invoke CI. The default token is read-only; only the security caller
receives the permissions needed to upload analysis. No secrets are inherited
or publishing workflows invoked. Dependabot proposes grouped updates to pinned
Actions and reusable workflows for review.

The Makefile's migration target must use a disposable `CI_POSTGRES_URL`; it
performs a downgrade/upgrade check before integration tests write halt history.
The test target refuses to run without that URL, rather than silently skipping
the mandatory PostgreSQL tests.

```sh
cd backend
uv run --frozen pytest --no-cov tests/security tests/trading
# Use a disposable PostgreSQL database; tests create isolated schemas.
TEST_DATABASE_URL=postgresql://localhost/safety_tests \
  uv run --frozen pytest --no-cov tests/integration/test_trading_halt_postgres.py
cd ../frontend
npx playwright test --config playwright.safety.config.ts
```

Some legacy calculation/handler tests explicitly mock the release barrier along
with their fake database/exchange boundary to preserve regression coverage of
inactive code. The separate security suite never overrides that barrier and
checks disabled and all-enabled configurations. A submission inventory check
requires review when another direct CLOB submission module appears.

## Remaining specification work

| Issue | Status / remaining work |
| --- | --- |
| SEC-001 | Freeze and durable halt foundation implemented; authoritative position reconciliation and authorized reduce-only availability remain blocked by the execution work |
| RISK-001 | Pending: immutable Decimal multi-strategy ledger, fill evidence, balanced transactions and repeatable backfill |
| RISK-002 | Pending: reconciled capital, concentration, exposure, valuations, fees and persisted equity peak |
| RISK-003 | Pending: account locks, durable risk decisions, cash/inventory reservations and freshness enforcement |
| EXEC-001 | Pending: execution intent/order schema and evidence-based transitions |
| EXEC-002 | Pending: PostgreSQL idempotency, payload conflict handling and unknown-submission recovery |
| EXEC-003 | Pending: execution gateway, submission outbox, worker fencing and migration of every strategy |
| EXEC-004 | Pending: authoritative maker quote diff/replacement and cancellation reconciliation |
| SEC-002 | Pending: isolated signer, real exchange payload authorization and custody boundary |
| SEC-003 | Pending: complete permission matrix, step-up auth, credential lifecycle, log redaction and threat model |
| EXIT-001 | Pending: durable conditional trigger/execution states and shared inventory reservations |
| EXIT-002 | Tracked emergency halt/cancellation foundation implemented; actual exit execution, quantities and verified closure remain pending |
| QA-001 | Stage-0 fault tests implemented; ledger invariants, property tests and full exchange simulator remain pending |
| QA-002 | Stage-0 checks and inherited CodeQL/dependency/workflow analysis added; remaining secret scanning, provenance, Redis-failure/canary gates and independent review pending |
| FIN-001 | Pending: follower execution simulation and insufficient-data backtest status |
| FIN-002 | Pending: executable-depth complement evaluation, fees, settlement validity and leg-risk rejection |

Next release: RISK-001, followed by RISK-002/RISK-003 and EXEC-001/EXEC-002.
Do not remove the legacy release barrier when adding shadow accounting.
