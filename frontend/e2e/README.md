# E2E suite

Feature-by-feature end-to-end tests for the Polymarket app, driven with
[Playwright](https://playwright.dev) plus the
[WebMCP](https://github.com/webmcp/spec) polyfill for the in-page agent
surface.

## Running

```bash
# from frontend/
npm run test:e2e          # full suite (starts both servers if not running)
npm run test:e2e -- --project=public    # public pages only
npm run test:e2e -- e2e/features/dashboard.spec.ts
npx playwright test --ui  # interactive
```

The Playwright config (`frontend/playwright.config.ts`) manages both servers
itself and reuses anything already running:

- Vite dev server on `http://localhost:5174` (proxying `/api`), and
- the FastAPI backend on `http://127.0.0.1:8002`, started from
  `backend/.venv` with `PYTHONPATH` including `services/shared`.

### Local environment the suite expects

The suite talks to a real stack, so it needs:

- PostgreSQL on `localhost:5432` with the `polymarket` database, migrations
  applied (`backend/.venv/bin/alembic upgrade head`).
- `backend/.env` (gitignored) with a test-friendly `DATABASE_URL`,
  `ENVIRONMENT=development`, `ROLE=web`, empty `REDIS_URL` (in-memory cache
  and challenge store are used in local dev) and — recommended while
  iterating — `RATE_LIMIT_PER_MINUTE=1000`, because the suite issues more
  traffic than the production default of 100/min per IP allows and a 429 on
  `/api/auth/me` logs the page out mid-run.
- `frontend/.env` with an empty `VITE_API_URL` so the dev server proxies
  `/api` to the backend.

## What is covered

| Project    | Area                                                                                                                                                                                                                                                      |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `public`   | Markets (categories, search, sort, detail drawer, trade gate), Opportunities, Leaderboard (periods, table size, follow gate), Your Guide + tutorial overlay, AI Analysis (incl. degraded error state), Copy Trading landing, nav + unknown-route 404 page |
| `auth`     | Wallet-signature login (`personal_sign` with a mock EIP-1193 provider), missing wallet, rejected signature, keep-logged-in, logout confirmation, route guards (anonymous redirects, admin-only `/debug`, signed-in users skip `/login`)                   |
| `features` | Dashboard, Trades, Whales, Execution analytics, Calibration, Risk report, Latency Arb, Cross-venue signals, AI Orchestrator, Ask the AI, Ops Center, Settings (all tabs), Copy Trading, Market Making, Backtesting, News, and the WebMCP agent surface    |

## Sessions

`e2e/global-setup.ts` signs in two wallets once and stores their sessions in
`e2e/.auth/*.json`:

- **admin** — Hardhat account #0, listed in the backend's `ADMIN_WALLETS`
  (it is promoted at backend startup, so restart the backend after this
  wallet's first login).
- **user** — Hardhat account #2, a regular user.

Both keys are the publicly documented Hardhat/Anvil development keys and
carry no value. The authenticated fixtures (`adminPage` / `userPage`) share
one worker-scoped browser context per role: the backend rotates refresh
tokens on every refresh and rate-limits the auth endpoints, so per-test
contexts would keep invalidating each other's session thread.

## WebMCP

`e2e/features/webmcp.spec.ts` injects `@mcp-b/webmcp-polyfill` before the app
code on every page under test, so `document.modelContext` exists exactly as
it would in a WebMCP-enabled browser. The tests assert on the agent-facing
contract — `getTools()`, `registerTool()`, `executeTool()`, `toolchange`,
duplicate-name rejection, unregistration via `AbortSignal` — and finish with
a tool registered on the live Markets page that reads real DOM state and is
cross-checked against an independent Playwright query.

The app registers no WebMCP tools yet; the suite pins the surface an agent
would rely on, and `the app ships no built-in tools` documents that baseline.

## Required GitHub Actions E2E gate

The root `.github/workflows/ci.yml` has a **required full-stack `e2e` job** for
pushes to `main`, pull requests and merge groups. It installs the pinned
backend/frontend dependencies and Chromium, starts an isolated PostgreSQL 16
service, runs Alembic to head, seeds the well-known **public development-only**
Hardhat admin wallet and launches both services using the existing Playwright
webServer config. All three projects (`public`, `auth`, `features`) run;
failures fail the top-level `Quality gate`. No E2E test is skipped or made
non-blocking just because a public provider is unavailable. The existing
`emergency-browser` job still independently tests the emergency-halt contract.

A fresh random JWT secret is generated for each CI job. The Postgres password
only authenticates the ephemeral service inside its disposable runner; no
production credentials or funded wallets are loaded. Failed runs upload
Playwright traces/screenshots and a JUnit report as short-lived artifacts.
This is a real-stack browser test using disposable local persistence, **not**
a live venue-order E2E test. A passing run does not prove real-money behavior.

If a PR changes market feeds or introduces external dependencies, prefer
recorded deterministic fixture responses in feature tests, not suppressing
assertions or depending on undocumented public rate-limit windows.

## Credentialed user workflows

The ordinary public/auth/features suite remains available on every PR without provider secrets.
`Real user workflows / live providers` additionally runs on trusted main pushes, scheduled runs and
manual CI dispatches. It is required by the quality gate on those triggers. It is intentionally not
run on pull requests/merge groups, and never uses `pull_request_target` with provider credentials.

Configure these **Actions secrets** for the repository:

- `REAL_UX_OPENAI_API_KEY`: the OpenAI key used by the app's selected provider.
- `REAL_UX_TAVILY_API_KEY`: the app's research search credential.
- `REAL_UX_NEWSAPI_API_KEY`: the app's news research credential.

The job fails if any key is missing or the provider returns an error; it does not silently skip tests.
It starts the actual LLM gRPC service from its own frozen dependency lock, waits for health/capabilities,
and runs real authenticated API and browser requests against a fresh PostgreSQL schema. Three bounded
flows verify quick analysis, streamed browser completion and research-backed analysis containing real
Tavily sources and NewsAPI headlines. These checks do not mock routes or replace provider responses.

To run locally, first install the two locked Python environments and browser dependencies:

```sh
uv sync --project backend --frozen --python 3.11
uv sync --project services/llm-chain --frozen --python 3.11
cd frontend
npm ci
npx playwright install chromium
cd ..
UX_TEST_DATABASE_URL=postgresql:///postgres \
  uv run --project backend --frozen python scripts/run-real-ux.py --env-file /absolute/path/to/.env
```

The env-file reader copies only the three required provider keys. It never copies the production
wallet private key, GitHub token, session secret or database URL. `UX_TEST_DATABASE_URL` must be
explicit; the runner creates and removes a random schema and generates fresh test-session secrets.
Ports 8002, 5174 and 59151 must be free; existing servers are never reused. Provider calls use
`gpt-4o-mini`, 1,536 output tokens, disabled tier escalation and no browser retries.

RAG corpus validation, CLI/Copilot-provider success, other model providers and actual trading are
not claimed by this suite. No wallet capable of spending user funds is provided. API/backend errors
and provider outages remain failures. Redacted service diagnostics are kept separately from
Playwright's output directory so its automatic cleanup cannot erase startup failures.
