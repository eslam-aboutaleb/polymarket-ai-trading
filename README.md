# Polymarket AI Trading Automation

AI-assisted trading automation for [Polymarket](https://polymarket.com). The system
tracks leading traders on the Polymarket leaderboard, analyses markets and trader
behaviour with LLM backends, and executes copy-trade, contrarian, market-making and
stop-loss/take-profit strategies on a user's Polygon wallet.

> **This repository ships exactly one Markdown document at its root — this file.**
> Working notes, owner handbooks and archived planning documents live under `docs/`
> and are intentionally untracked (see [Documentation](#documentation)).

---

## Table of contents

- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Technology stack](#technology-stack)
- [Prerequisites](#prerequisites)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Authentication](#authentication)
- [Trading strategies](#trading-strategies)
- [API reference](#api-reference)
- [Development](#development)
- [Code quality and pre-commit](#code-quality-and-pre-commit)
- [Testing](#testing)
- [Security notes](#security-notes)
- [Operations and troubleshooting](#operations-and-troubleshooting)
- [Documentation](#documentation)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         Browser (React 18 + TypeScript)                        │
│  Dashboard · Markets · Opportunities · Copy Trading · Market Making          │
│  Stop Loss / Take Profit · Inverse Bot · Backtesting · News · Leaderboard     │
│  Whale Feed · Latency Arb · Execution Analytics · Notification Center         │
│  Settings · Ops Center (admin) · WalletConnect / injected wallet             │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │ HTTPS  /api/*   (same-origin in prod)
┌──────────────────────────────────────▼───────────────────────────────────────┐
│                       nginx  (production)  ·  :5173 dev                       │
│   static assets + security headers + reverse proxy  /api/  →  backend        │
└──────────────────────────────────────┬───────────────────────────────────────┘
                                       │
┌──────────────────────────────────────▼───────────────────────────────────────┐
│                          FastAPI backend  ·  :8000                            │
│                                                                              │
│  api/routes/     auth · analysis · markets · trades · portfolio · settings    │
│                  news · backtesting · inverse-bot · market-maker · binance   │
│                  debug (admin only)                                           │
│  security/       JWT issuance/verification · Fernet credential vault         │
│                  refresh-token hashing · EIP-191 signature recovery          │
│  services/       polymarket_service (py-clob-client) · copy_trade_service    │
│                  market_maker_service · trade_monitor · stop_loss_monitor    │
│                  inverse_bot_monitor · arbitrage · leaderboard · news · …     │
│                  whale_monitor · latency_arb · kelly_sizing · redemption     │
│                  pnl_reconciliation · pre_trade_gate · alert_service · …      │
│  llm/            LangChain assessment chains (web-search-augmented)          │
│  grpc_clients/   pooled gRPC client for the AI backends                      │
└───────┬──────────────────────────────────┬────────────────────────────────────┘
        │                                  │
        │ gRPC :50051 / :50052             │ SQL
┌───────▼──────────────────────┐  ┌────────▼─────────────────────────────────┐
│  llm-chain  ·  cli-agent      │  │  PostgreSQL :5432   ·   Redis :6379     │
│  Python AI microservices      │  │  27 SQLAlchemy models · 14 migrations    │
│  LLM providers + MCP research │  │  token/credential cache · rate limiting  │
└───────────────────────────────┘  └──────────────────────────────────────────┘
```

**Request path in production** is deliberately same-origin: the browser issues
relative `/api/...` requests, nginx proxies them to the backend, and cookies stay
first-party. This removes CORS and cross-origin credential handling from the
production path entirely.

### AI backends (gRPC)

Both services implement the `AnalysisService` contract defined in
`services/proto/analysis.proto` and are selected via `DEFAULT_AI_BACKEND`:

| Service            | Port   | Role                                                             |
| ------------------ | ------ | ---------------------------------------------------------------- |
| `llm-chain`        | `50051`| LangChain pipelines, multi-provider LLM access, streaming analysis |
| `cli-agent`        | `50052`| CLI-driven agent with MCP tools (web research)                    |

RPCs include `AnalyzeMarket`, `QuickAnalysis`, `ScanMarkets`, `AssessRisk`,
`GenerateTradePlan`, `AnalyzeMarketStream`, `AnalyzeTrader`, `EvaluateCopyTrade`,
`EvaluateInversePosition`, `AnalyzeSentiment`, `DiscoverBestTrade`,
`IndexMarketsRAG`, `GenerateNews`, `GenerateNewsBatch` and `HealthCheck`.

The backend keeps a **pooled, shared gRPC channel** per address
(`app/grpc_clients/analysis_client.py`) rather than opening a channel per request.

---

## Repository layout

```
polymarket/
├── backend/                      FastAPI service
│   ├── app/
│   │   ├── main.py               application entry point, lifespan, middleware
│   │   ├── config.py             pydantic-settings configuration
│   │   ├── api/routes/           17 routers
│   │   ├── models/               27 SQLAlchemy models
│   │   ├── schemas/              Pydantic request/response models
│   │   ├── security/             auth.py · crypto.py · credential_store.py
│   │   ├── services/             trading, monitoring and data services
│   │   ├── llm/                  LangChain assessment chains
│   │   ├── grpc_clients/         pooled gRPC client (+ generated stubs)
│   │   ├── repositories/         data-access helpers
│   │   ├── middleware/           request logging
│   │   └── utils/                cache · database · indicators · time
│   ├── migrations/               Alembic (14 revisions)
│   ├── tests/                    pytest suite
│   ├── pyproject.toml            dependencies + ruff + pytest config
│   └── Dockerfile / .prod
│
├── frontend/                     React 18 + Vite SPA
│   ├── src/
│   │   ├── components/           36 components
│   │   ├── pages/                route-level pages
│   │   ├── services/             typed API clients
│   │   ├── store/authStore.ts    in-memory Zustand session state
│   │   ├── hooks/ · utils/ · types/ · context/
│   │   └── config/api.ts         API origin resolution
│   ├── nginx.conf                production static server + /api proxy
│   └── package.json
│
├── services/
│   ├── llm-chain/                LangChain gRPC microservice
│   ├── cli-agent/                CLI-agent gRPC microservice
│   ├── mcps/research/            MCP research server
│   └── proto/analysis.proto      shared gRPC contract
│
├── prompts/                      prompt templates (prompts.json)
├── docs/                         local study notes — NOT tracked
├── docker-compose.yml            local development stack
├── docker-compose.prod.yml       production stack
├── start.sh                      local dev launcher
├── ruff.toml                     Ruff config for backend + services
├── .pre-commit-config.yaml       ruff · prettier · repo hygiene
├── .prettierrc.json              frontend formatting rules
└── README.md                     this file
```

---

## Technology stack

**Backend** — Python 3.11+, FastAPI, Uvicorn, SQLAlchemy 2.x, Alembic,
Pydantic v2 / pydantic-settings, `python-jose` (JWT), `cryptography` (Fernet),
`py-clob-client` + `web3` / `eth-account` (Polymarket + Ethereum), `httpx`,
`slowapi` (rate limiting), `redis`, `grpcio`.

**AI services** — LangChain, LangChain Community, OpenAI SDK, MCP research tooling.

**Frontend** — React 18, TypeScript 5, Vite 5, React Router 6, TanStack Query v3
(`react-query`), Zustand, React Hook Form, Axios, Tailwind CSS 3, Recharts,
React Virtuoso, Ethers + WalletConnect.

**Infrastructure** — PostgreSQL, Redis, Docker, Docker Compose, nginx.

---

## Prerequisites

- Python **3.11+** and [uv](https://docs.astral.sh/uv/)
- Node.js **18+** and npm
- PostgreSQL **15+**
- Redis (optional locally; required in production for credential caching)
- Docker + Docker Compose (if running the full stack)
- A browser wallet (MetaMask, Rabby, Coinbase Wallet) or a WalletConnect wallet

---

## Quick start

### Docker Compose (full stack)

```bash
cp .env.example .env          # then edit: JWT_SECRET_KEY, OPENAI_API_KEY, …
docker compose up --build
```

| Service       | URL                            |
| ------------- | ------------------------------ |
| Frontend      | http://localhost:5173          |
| Backend API   | http://localhost:8000          |
| OpenAPI docs  | http://localhost:8000/docs     |
| PostgreSQL    | `localhost:5432`               |
| Redis         | `127.0.0.1:6379`               |
| llm-chain     | gRPC `localhost:50051`         |
| cli-agent     | gRPC `localhost:50052`         |

Production uses `docker-compose.prod.yml`, which serves the built SPA from nginx
on port 80 and proxies `/api/` to the backend.

### Local development

```bash
# Backend
cd backend
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload

# Frontend (second terminal)
cd frontend
cp .env.example .env
npm install
npm run dev
```

`./start.sh` performs the same steps interactively and starts Postgres via Docker
when it is not already running.

---

## Configuration

Configuration is environment-driven. `.env.example` and `.env.prod.example` are
committed templates; real `.env` files are gitignored.

### Security-critical variables

| Variable                      | Purpose                                                             |
| ----------------------------- | ------------------------------------------------------------------- |
| `JWT_SECRET_KEY`              | Signs access and refresh tokens. **Required**, min 32 chars, no placeholder. |
| `REFRESH_TOKEN_HASH_SECRET`   | HMAC key for hashing refresh tokens at rest.                         |
| `ENCRYPTION_MASTER_KEYS`      | Comma-separated Fernet keyring for encrypting wallet private keys.  |
| `CREDENTIAL_ENCRYPTION_BACKEND` | Envelope backend for stored credentials: `fernet` (default) or `gcp_kms`. |
| `GCP_KMS_KEY_NAME`            | Full GCP KMS key resource path (`projects/PROJECT/locations/LOCATION/keyRings/RING/cryptoKeys/KEY`), required when `CREDENTIAL_ENCRYPTION_BACKEND=gcp_kms`. |
| `REQUIRE_REDIS_FOR_CREDENTIALS` | Fail instead of silently using an in-process credential cache.     |
| `ADMIN_WALLETS`               | Comma-separated wallet addresses granted admin.                      |
| `ENVIRONMENT`                 | `production` enables secure cookies, disables debug endpoints and stack-trace leakage. |

### Database, cache and networking

`DATABASE_URL`, `REDIS_URL`, `REDIS_PASSWORD`, `CREDENTIAL_CACHE_TTL_SECONDS`,
`CORS_ALLOWED_ORIGINS`, `HOST`, `PORT`.

### LLM providers

`LLM_PROVIDER`, `LLM_MODEL`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS`,
`DEFAULT_AI_BACKEND`, `GRPC_TIMEOUT`, plus credentials for any of
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY`,
`OLLAMA_BASE_URL`, `GITHUB_TOKEN`, `TAVILY_API_KEY`.

### Feature flags and observability

`BINANCE_SKILLS_ENABLED`, `NEWSAPI_ENABLED`, `TAVILY_ENABLED`, `RAG_ENABLED`,
`POLYMARKET_PRIVATE_KEY`, `POLYMARKET_CHAIN_ID`, `LOG_LEVEL`,
`DEBUG_ENDPOINTS_ENABLED`, `DEBUG_LOG_HASH_SALT`, `ADMIN_SYNC_ALLOW_EMPTY`.

### Frontend variables

| Variable                        | Notes                                                          |
| ------------------------------- | -------------------------------------------------------------- |
| `VITE_API_URL`                  | **Empty in production** → same-origin `/api` via nginx. Absolute `https://origin` only if the API is genuinely cross-origin. A value of `/api` is wrong — service calls already include `/api`. |
| `VITE_WALLETCONNECT_PROJECT_ID` | Required for the WalletConnect QR login flow.                  |

> Anything prefixed `VITE_` is **inlined into the public browser bundle**. Never
> place a secret in a `VITE_` variable.

---

## Authentication

Two login paths are implemented.

**Wallet signature (preferred).** The frontend requests a challenge from
`POST /api/auth/login`, the wallet signs it with `personal_sign`, and the backend
recovers the address using EIP-191 (`encode_defunct`) and compares checksummed
addresses. No key material is involved.

**Private key login.** `POST /api/auth/login-with-key` accepts a raw private key
directly so orders can be signed server-side. This key is encrypted at rest with
Fernet (with key rotation) and is purged on logout.

Session handling:

- Short-lived **JWT access token** plus a longer-lived **refresh token**.
- Refresh tokens are **HMAC-hashed at rest**, rotated on use, single-use and
  revocable (`jti`).
- Tokens are delivered as **httpOnly, SameSite=strict cookies** in addition to the
  JSON body; `Secure` is set automatically outside local environments.
- Logout revokes the refresh token and deletes stored wallet credentials.

Session state lives **in memory only** (Zustand) — never in `localStorage`.

---

## Trading strategies

| Capability              | Endpoint prefix        | Description                                                                     |
| ----------------------- | ---------------------- | ------------------------------------------------------------------------------- |
| Manual execution        | `/api/trades`          | Explicit limit orders via `POST /api/trades/execute`; cash-out, history, P&L.   |
| Copy trading            | `/api/trades`          | Mirror followed traders with risk modes, daily-loss limits and safety caps.     |
| Market making           | `/api/market-maker`    | Bands and AMM strategies with configurable spread, collateral and sync interval. |
| Inverse bot             | `/api/inverse-bot`     | Contrarian positions sized by configured mode, evaluated by the AI backend.     |
| Stop loss / take profit | `/api/trades`          | Per-position resting orders, evaluated on a price-order cycle.                  |
| Arbitrage               | `/api/trades`          | Cross-market opportunity scanning.                                              |
| Backtesting             | `/api/backtesting`     | Replays copy-trade and indicator strategies over historical data.               |
| News generation         | `/api/news`            | LLM-generated news items and feeds, streamed over SSE.                          |
| Whale monitoring        | `/api/whales`          | Tracks large wallet movements and configurable whale alerts.                    |
| Latency arbitrage       | `/api/latency-arb`     | Exploits price lag between venues with paper-trading and safety gates.          |

Safety controls on the automated path include a global trading halt, per-user
cooldown, monthly and daily loss limits, maximum drawdown, total-loss halt, and
maximum position size. `POST /api/trades/emergency-stop` halts trading and can
market-close all positions; `POST /api/trades/resume-trading` reverses it.

---

## API reference

Interactive documentation is served at `/docs` (Swagger UI) and `/redoc`.

### Authentication — `/api/auth`
`POST /login` · `POST /login-with-key` · `POST /refresh` · `POST /logout` ·
`GET /me`

### Analysis — `/api/analysis`
`POST /market` · `POST /market/stream` · `GET /market/{condition_id}` ·
`POST /quick` · `POST /quick-group` · `POST /scan` · `POST /opportunities/stream` ·
`POST /opportunities/analyze-markets` · `POST /trader-analysis/stream` ·
`POST /trade-plan` · `GET /health`

### Markets — `/api/markets`
`GET /markets` · `GET /markets/combined` · `GET /markets/newest` · `GET /search` ·
`GET /categories` · `GET /browse`

### Trades — `/api/trades`
`POST /execute` · `POST /cash-out` · `GET /history` · `POST /trader` ·
`POST /follow/{wallet}` · `DELETE /follow/{wallet}` · `GET /following` ·
`GET /following-feed` · `GET /feed` · `GET /copy-trades` · `GET /copy-trades/pnl` ·
`GET /copy-trading` · `PUT /copy-trading` · `GET /copy-evaluation/{wallet}` ·
`POST /copy-trade-eval` · `GET/POST /stop-loss` · `DELETE /stop-loss/{id}` ·
`GET/POST /take-profit` · `DELETE /take-profit/{id}` · `POST /emergency-stop` ·
`POST /resume-trading` · `GET /risk`

### Portfolio — `/api/portfolio`
`GET /balance` · `GET /positions` · `GET /positions/prices` ·
`GET /markets/prices` · `GET /dashboard` · `GET /summary`

### Settings — `/api/settings`
`GET/PUT /` · `GET /llm/current` · `PATCH /llm` · `GET /llm/providers` ·
`GET /backends/status` · admin-only: `GET /admin/providers`,
`GET /admin/users`, `PATCH /admin/users/{user_id}/llm`, `PATCH /admin/defaults`

### News — `/api/news`
`POST /generate` · `POST /generate/stream` · `POST /refresh-feed`

### Backtesting — `/api/backtesting`
`GET /strategies` · `GET/POST /runs` · `GET /runs/{run_id}` ·
`DELETE /runs/{run_id}`

### Market maker — `/api/market-maker`
`GET/POST /configs` · `DELETE /configs/{config_id}` ·
`POST /configs/{config_id}/start` · `/stop` · `/sync`

### Inverse bot — `/api/inverse-bot`
`GET/POST /positions` · `DELETE /positions/{position_id}` ·
`POST /positions/{position_id}/evaluate`

### Binance signals — `/api/binance`
`GET /dashboard` · `GET /signals/smart-money` · `GET /signals/active-buys` ·
`GET /rankings/social-hype` · `/trending` · `/smart-money-inflow` ·
`/pnl-leaderboard` · `GET /token/search` · `GET /token/data`

### Whale monitoring — `/api/whales`
`GET /events` · `GET /config` · `PUT /config`

### Latency arbitrage — `/api/latency-arb`
`GET /config` · `POST /config` · `GET /opportunities` · `GET /trades`

### Notifications — `/api/notifications`
`GET /channels` · `POST /channels` · `DELETE /channels/{channel_id}` ·
`GET /` · `PATCH /{event_id}/read` · `GET /preferences` ·
`PUT /preferences` · `POST /test`

### Health & metrics — `/health`
`GET /health` · `GET /metrics`

### Trading key — `/api/auth`
`POST /trading-key` · `GET /trading-key` · `DELETE /trading-key`

### Debug (admin only, disabled unless `DEBUG_ENDPOINTS_ENABLED=true`)
`GET /health` · `GET /metrics` · `GET/DELETE /logs`

---

## Development

### Backend

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload     # http://localhost:8000
```

Add a database change with Alembic:

```bash
uv run alembic revision --autogenerate -m "describe the change"
uv run alembic upgrade head
```

### Frontend

```bash
cd frontend
npm run dev            # vite dev server
npm run build          # tsc -b && vite build
npm run type-check     # tsc --noEmit
npm run format         # prettier --write
npm run format:check   # prettier --check
```

> **Config-file gotcha.** `frontend/tsconfig.node.json` emits build artefacts to
> `node_modules/.tmp/tsconfig.node`. Do not remove that `outDir`: when
> `tsc -b` writes `vite.config.js` next to `vite.config.ts`, Vite prefers the
> emitted `.js` and silently ignores edits to the TypeScript source.

---

## Code quality and pre-commit

The repository pins its formatters and linters through
[`.pre-commit-config.yaml`](.pre-commit-config.yaml).

| Hook          | Applies to                     | What it enforces                                              |
| ------------- | ------------------------------ | ------------------------------------------------------------- |
| `ruff-check`  | `backend/`, `services/`        | pyflakes, pycodestyle, isort, pyupgrade, bugbear, bandit, simplifications |
| `ruff-format` | `backend/`, `services/`        | Black-compatible formatting at 100 columns                     |
| `prettier`    | `frontend/`                    | Consistent frontend formatting                                  |
| `jscpd`       | `backend/`, `services/`, `frontend/` | Total duplication must stay under **3%**                 |
| hygiene hooks | repository                     | trailing whitespace, EOF newline, merge conflicts, YAML validity, large files, private-key detection |

Ruff's `bandit` (`S`) rules are enabled deliberately — this codebase handles
private keys, signs real orders and executes SQL, so security lint is part of the
gate.

Lint and format rules live in a single repository-wide [`ruff.toml`](ruff.toml);
`backend/pyproject.toml` inherits it via `extend`, so results are identical
whether Ruff is run from the repository root or from `backend/`. Frontend
formatting rules live in [`.prettierrc.json`](.prettierrc.json).

Ruff and Prettier are invoked through the tools the project itself installs
(`uv.lock` and `package.json`), so a hook can never disagree with a local run.

### SonarQube

[`sonar-project.properties`](sonar-project.properties) configures analysis and
records the quality gate:

| Metric               | Target |
| -------------------- | ------ |
| Duplicated lines     | < 3%   |
| Line coverage        | ≥ 80%  |
| New code issues      | 0      |
| CRITICAL / BLOCKER   | 0      |

```bash
cd backend && uv run pytest          # writes backend/coverage.xml for the scanner
sonar-scanner -Dsonar.projectKey=polymarket
```

Lint and duplication are also enforced locally and in pre-commit, so most
regressions can be caught without a SonarQube server.

### Duplication budget

`scripts/check-duplication.sh` runs `jscpd`, which implements the same
token-based algorithm as SonarQube's CPD, and fails above 3%. Override the
threshold with `MAX_DUPLICATION=5 ./scripts/check-duplication.sh`.

Current shared abstractions worth knowing before adding code:

- `services/shared/polymarket_mcp/` — the research MCP client used by both AI
  services (it was duplicated and had drifted).
- `frontend/src/services/sseStream.ts` — SSE consumption shared by the analysis,
  markets and news clients.
- `frontend/src/components/PriceTriggerModal.tsx` — the stop-loss and take-profit
  modals, which are one component with opposite direction.

Install the hooks once per clone:

```bash
pre-commit install
```

Run across the whole repository:

```bash
pre-commit run --all-files
```

Run the same checks directly:

```bash
uv run --project backend --frozen ruff check --config ruff.toml backend services
uv run --project backend --frozen ruff format --check --config ruff.toml backend services
npx prettier --check "frontend/**/*.{ts,tsx,css,json,html}"
```

---

## Testing

Coverage is produced by `pytest-cov` and written to `backend/coverage.xml` for
SonarQube.

```bash
cd backend
uv run pytest                      # full suite + coverage report
uv run pytest tests/test_security_config.py -v
uv run pytest --cov=app.services.polymarket_service --cov-report=term-missing
```

Every module and frontend file carries a header docstring describing its role, so
the test layout, the request path and the shared helpers can be understood from
the file itself.

---

## Risk controls and known gaps

Safety controls on the automated copy-trade path include a global trading halt,
per-user cooldown, monthly and daily loss limits, maximum drawdown, total-loss
halt, and maximum position size. `POST /api/trades/emergency-stop` halts trading
and can market-close all positions; `POST /api/trades/resume-trading` reverses
it, including the copy-trading kill switch.

### Realized PnL

Every loss limit derives from `UserTrade.pnl`. That column is written **only** by
`app/services/pnl_reconciliation.py`, which matches sell trades against open buy
lots FIFO and records realized profit/loss on each closing sell. It runs for every
user on each position-lifecycle cycle (`LIFECYCLE_CHECK_INTERVAL`, five minutes).

### Pre-trade gate, Kelly sizing and paper trading

Automated strategies pass through `app/services/pre_trade_gate.py`, which
re-checks the halt, cooldown and loss limits immediately before an order is
signed — the last line of defence between a stale cached decision and a real
order. Position sizing can use the Kelly criterion
(`app/services/kelly_service.py`) to scale size to the modelled edge. Every
automated strategy (latency arb, inverse bot, market maker) supports a
**paper-trading mode** that walks the full decision and order-construction path
against live prices but never signs or submits an order, so new strategies can
be validated against real market data with zero capital at risk.

### Redemption and execution analytics

`app/services/redemption_service.py` tracks conditional-token redemption
attempts (the CTF redeem flow) with per-attempt persistence, and
`app/services/execution_analytics.py` records fill quality — slippage,
fill latency and price impact — for every executed order, surfaced through the
Execution Analytics dashboard.

Before this existed nothing wrote `pnl`, so the daily-loss, monthly-loss,
drawdown and total-loss layers all evaluated against a permanent zero and the bot
had **no working loss circuit breaker**. If you operate this system, confirm
`GET /api/settings/admin/providers`-style reconciliation output is non-trivial —
a user with executed trades but zero PnL everywhere means reconciliation is not
running.

### Operational guidance

Scheduled jobs run behind Postgres advisory locks with heartbeats
(`app/utils/scheduler_lock.py`), and trade-event deduplication is shared
through the cache backend, so the backend scales to multiple workers
without duplicate monitors or duplicate orders. A dead-man's-switch
watchdog in the lifespan alerts if the scheduler loop stops heartbeating.

---

## Security notes

- Private keys are encrypted at rest with Fernet under a configurable keyring and
  are never logged; refresh tokens are hashed before storage.
- All database access goes through the SQLAlchemy ORM with bound parameters.
- Production serves the SPA from the same origin as the API, so cookies remain
  first-party and CORS is not part of the production request path.
- nginx sets `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` and
  `Permissions-Policy`; the Vite build injects a restrictive CSP.
- Rate limiting is applied globally through `slowapi` at 100 requests/minute per
  client IP.
- Keep `ENVIRONMENT=production` in every deployed environment. It is the single
  switch that enables secure cookies, disables the debug router and stops raw
  exception text from reaching clients.

---

## Operations and troubleshooting

**Backend will not start** — confirm PostgreSQL is reachable, that `uv sync`
completed, and that `DATABASE_URL` and `JWT_SECRET_KEY` are set.

**Frontend cannot reach the API in development** — the backend must be listening
on `http://localhost:8000`, `VITE_API_URL` must be set in `frontend/.env`, and
`CORS_ALLOWED_ORIGINS` must include the frontend origin. In development Vite
proxies `/api` to the backend.

**Production returns 404 on every API call** — `VITE_API_URL` must be empty
(same-origin) or a bare `https://origin`. Setting it to `/api` produces
`/api/api/...` paths.

**Migrations fail** — run `uv run alembic upgrade head` from `backend/` and read
the reported revision; `alembic.ini` and `migrations/` are excluded from linting
by design.

**Signature verification fails** — reconnect the wallet, request a fresh
challenge, and confirm the wallet supports `personal_sign`. Set
`VITE_WALLETCONNECT_PROJECT_ID` if the QR flow is required.

**AI backend unhealthy** — `GET /api/settings/backends/status` reports health per
backend; check that the gRPC services are listening on 50051 / 50052 and that the
required provider API keys are configured.

---

## Documentation

This `README.md` is the only tracked Markdown file in the repository.

Study notes, the owner handbook and superseded planning documents are stored under
`docs/` (including `docs/owner-handbook/` and `docs/study/`) and are **excluded
from version control** by `.gitignore`. They remain available locally and on the
deploying machine but are not part of the repository history.

---

## License

MIT

## Support

Please open an issue on GitHub for questions or problems.
