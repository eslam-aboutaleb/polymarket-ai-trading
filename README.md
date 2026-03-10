# Polymarket AI Trading Automation

An AI-powered trading automation system for Polymarket that identifies winning traders, assesses trade opportunities using LangChain + OpenAI, and enables copying or contrarian trading strategies.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      React Frontend (TypeScript)                     │
│  ┌─────────────────┬──────────────────┬──────────────────────────┐  │
│  │ Login (Wallet)  │ Dashboard        │ Settings / Analytics     │  │
│  │ - Web3 Sign     │ - Portfolio      │ - Execution Mode         │  │
│  │ - Keep Logged   │ - Opportunities  │ - Logout                 │  │
│  │   In (90 days)  │ - Leaderboard    │                          │  │
│  └─────────────────┴──────────────────┴──────────────────────────┘  │
└──────────────────────────────┬──────────────────────────────────────┘
                                │ HTTP/JSON
┌──────────────────────────────┴──────────────────────────────────────┐
│                      FastAPI Backend (Python)                        │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ API Routes                                                  │    │
│  │ - /api/auth/* (Login, Verify, Refresh, Logout)            │    │
│  │ - /api/opportunities (Detect & assess opportunities)      │    │
│  │ - /api/trades/* (Execute, history)                        │    │
│  │ - /api/winners/* (Leaderboard, follow)                    │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ Services                                                    │    │
│  │ - PolymarketSDK (py-clob-client)                           │    │
│  │ - LangChain Chain (Assessment)                             │    │
│  │ - Web Search MCP (Context)                                 │    │
│  │ - Binance Skills Hub (Smart Money + Rankings)              │    │
│  │ - Trade Execution                                          │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ Database Layer (SQLAlchemy)                                 │    │
│  │ - Users, RefreshTokens                                      │    │
│  │ - Markets, Winners, TradeHistory                            │    │
│  │ - Assessments, UserTrades                                   │    │
│  └─────────────────────────────────────────────────────────────┘    │
└──────────────────────────────┬──────────────────────────────────────┘
                                │
                ┌───────────────┼───────────────┐
                │               │               │
        ┌──────▼────────┐   ┌──▼────────────┐  │
        │  PostgreSQL   │   │ Polymarket    │  │
        │  (Users,      │   │ API           │  │
        │   Trades, etc)│   │ -Markets      │  │
        └───────────────┘   │ -Leaderboard  │  │
                            │ -Pricing      │  │
                            └───────────────┘  │
                                              │
                            ┌─────────────────▼──┐
                            │   OpenAI API      │
                            │   - GPT-4 Turbo   │
                            │   - Analysis      │
                            └───────────────────┘
```

## Features

### Authentication

- **Wallet-based login** via Ethereum signature verification
- **JWT access tokens** (15 min expiry)
- **Refresh tokens** with configurable session length (7 or 90 days)
- **Keep me logged in** checkbox for persistent sessions
- **Secure logout** with token revocation

### Opportunity Detection

- Monitor top traders on Polymarket leaderboard
- Detect winning patterns and recent trades
- Identify mispriced market outcomes
- AI assessment using LangChain + OpenAI GPT-4

### AI Assessment

- LangChain chains for structured trade analysis
- Web search MCP integration for market context
- **Binance Skills Hub integration** for smart money signals, social hype, and token data
- Risk assessment (low/medium/high)
- Confidence scoring (0-100)
- Market sentiment analysis

### Trade Execution

- **Configurable execution mode**: Auto or approval-required
- **Position sizing limits**: Prevent excessive risk
- **Slippage tolerance**: Control execution prices
- **Trade history tracking**: All executed trades logged
- **P&L calculation**: Automatic profit/loss tracking

## Project Structure

```
polymarket/
├── backend/                      # Python FastAPI backend
│   ├── app/
│   │   ├── main.py             # FastAPI application entry point
│   │   ├── config.py           # Settings and configuration
│   │   ├── models/             # SQLAlchemy models
│   │   │   ├── base.py         # Base declarative model
│   │   │   ├── user.py         # User model
│   │   │   ├── token.py        # RefreshToken model
│   │   │   ├── market.py       # Market model
│   │   │   ├── winner.py       # Winner model
│   │   │   ├── trade_history.py # TradeHistory model
│   │   │   ├── assessment.py   # Assessment model
│   │   │   └── user_trade.py   # UserTrade model
│   │   ├── schemas/            # Pydantic request/response schemas
│   │   │   └── auth.py         # Authentication schemas
│   │   ├── api/
│   │   │   └── routes/
│   │   │       ├── auth.py     # Authentication routes
│   │   │       ├── binance_signals.py # Binance smart money API routes
│   │   │       └── ...         # Other route modules (coming soon)
│   │   ├── services/           # Business logic services
│   │   ├── llm/                # LangChain chains
│   │   ├── security/
│   │   │   └── auth.py         # JWT and signature verification
│   │   └── utils/
│   │       └── database.py     # Database initialization
│   ├── database/
│   │   └── migrations/         # Alembic migrations
│   ├── pyproject.toml          # UV dependencies
│   ├── .env.example            # Environment variables template
│   └── Dockerfile              # Docker configuration
│
├── frontend/                    # React TypeScript frontend
│   ├── src/
│   │   ├── main.tsx            # React entry point
│   │   ├── App.tsx             # Main component
│   │   ├── index.css           # Tailwind CSS
│   │   ├── pages/
│   │   │   ├── LoginPage.tsx   # Login page
│   │   │   └── DashboardPage.tsx # Main dashboard
│   │   ├── components/
│   │   │   ├── LoginForm.tsx   # Login form with wallet connection
│   │   │   ├── Navigation.tsx  # Navigation bar
│   │   │   └── Dashboard.tsx   # Dashboard content
│   │   ├── services/
│   │   │   ├── apiClient.ts    # Axios HTTP client with auth
│   │   │   ├── authService.ts  # Authentication API service
│   │   │   └── binanceSignalsService.ts # Binance smart money API service
│   │   ├── store/
│   │   │   └── authStore.ts    # Zustand auth store
│   │   ├── hooks/              # Custom React hooks
│   │   ├── utils/              # Utility functions
│   │   └── context/            # React context providers
│   ├── public/                 # Static assets
│   ├── package.json            # pnpm dependencies
│   ├── tsconfig.json           # TypeScript configuration
│   ├── vite.config.ts          # Vite configuration
│   ├── tailwind.config.js      # Tailwind CSS configuration
│   ├── postcss.config.js       # PostCSS configuration
│   ├── .env.example            # Environment variables template
│   └── Dockerfile              # Docker configuration
│
├── docker-compose.yml          # Docker Compose for local development
├── .gitignore                  # Git ignore rules
└── README.md                   # This file
```

## Setup Instructions

### Prerequisites

- Python 3.11+
- Node.js 18+
- pnpm (`npm install -g pnpm`)
- PostgreSQL 15+
- MetaMask/Rabby/Coinbase Wallet extension or WalletConnect-compatible wallet

### Backend Setup

1. **Initialize dependencies**:

   ```bash
   cd backend
   uv sync
   ```

2. **Configure environment**:

   ```bash
   cp .env.example .env
   # Edit .env with your settings:
   # - DATABASE_URL: PostgreSQL connection
   # - JWT_SECRET_KEY: Change to random secret
   # - POLYMARKET_PRIVATE_KEY: Your Polygon wallet private key
   # - OPENAI_API_KEY: Your OpenAI API key
   # - BINANCE_SKILLS_ENABLED: true (default) — enable Binance smart money signals
   ```

3. **Initialize database**:

   ```bash
   # Database will be initialized automatically when app starts
   # Or manually with: python -m alembic upgrade head
   ```

4. **Run backend**:
   ```bash
   uv run uvicorn app.main:app --reload
   # API available at http://localhost:8000
   # Docs at http://localhost:8000/docs
   ```

### Frontend Setup

1. **Install dependencies**:

   ```bash
   cd frontend
   pnpm install
   ```

2. **Configure environment**:

   ```bash
   cp .env.example .env
   # Configure:
   # - VITE_API_URL=http://localhost:8000
   # - VITE_WALLETCONNECT_PROJECT_ID=your_walletconnect_project_id (optional but required for QR login)
   ```

3. **Run frontend**:
   ```bash
   pnpm dev
   # Available at http://localhost:5173
   ```

### Using Docker Compose (Recommended)

```bash
cd /path/to/polymarket

# Set required environment variables
export POLYMARKET_PRIVATE_KEY="your_wallet_private_key"
export OPENAI_API_KEY="sk-your-openai-key"

# Start all services
docker-compose up

# Services will be available at:
# - Frontend: http://localhost:5173
# - Backend: http://localhost:8000
# - API Docs: http://localhost:8000/docs
# - Database: localhost:5432
```

## API Endpoints

### Authentication

- `POST /api/auth/login` - Get challenge message
- `POST /api/auth/verify` - Verify signature and get tokens
- `POST /api/auth/refresh` - Refresh access token
- `POST /api/auth/logout` - Logout and revoke token
- `GET /api/auth/me` - Get current user info

### Core Features (Coming Soon)

- `GET /api/opportunities` - List detected opportunities
- `POST /api/opportunities/{id}/assess` - AI assessment of trade
- `POST /api/trades/execute` - Execute a trade
- `GET /api/trades/history` - User's trade history
- `GET /api/winners/leaderboard` - Top traders
- `GET /api/markets/search` - Search markets
- `GET /api/portfolio` - User's positions and balance
- `GET/POST /api/settings` - User settings

### Binance Smart Money Signals

- `GET /api/binance/dashboard` - Aggregated signals dashboard (smart money, social hype, trending, inflow, PnL leaderboard)
- `GET /api/binance/signals/smart-money` - Smart money buy/sell signals by chain
- `GET /api/binance/signals/active-buys` - Tokens being accumulated by smart money
- `GET /api/binance/rankings/social-hype` - Social media hype rankings
- `GET /api/binance/rankings/trending` - Unified trending token rankings
- `GET /api/binance/rankings/smart-money-inflow` - Smart money net inflow rankings
- `GET /api/binance/rankings/pnl-leaderboard` - Top PnL traders (7d/30d)
- `GET /api/binance/token/search` - Search tokens by name/symbol
- `GET /api/binance/token/data` - Detailed dynamic data for a token

## Development Workflow

### Backend Development

1. Make changes to files in `backend/app/`
2. FastAPI will auto-reload with `--reload` flag
3. Check API docs at http://localhost:8000/docs

### Frontend Development

1. Make changes to files in `frontend/src/`
2. Vite will hot-reload automatically
3. Check console for errors (F12 Dev Tools)

### Database Changes

1. Create new models in `app/models/`
2. Generate migration: `alembic revision --autogenerate -m "description"`
3. Apply migration: `alembic upgrade head`

## Authentication Flow

1. **User connects wallet** → via Browser Wallet (injected provider) or WalletConnect
2. **Frontend captures connected address** → address is sourced from provider, not manual input
3. **Request challenge** → `POST /api/auth/login` returns challenge message
4. **User signs message** → wallet signs the challenge with `personal_sign`
5. **Verify signature** → `POST /api/auth/verify` with signature
6. **Backend verifies** → Recovers address from signature, creates/updates user
7. **Return tokens** → Access token (15 min) + Refresh token (7-90 days)
8. **Store tokens** → Frontend stores in Zustand state and localStorage
9. **API requests** → Include `Authorization: Bearer {access_token}` header
10. **Token refresh** → Automatically refresh before expiry using refresh token
11. **Logout** → Revoke refresh token, clear frontend state

## Next Steps

### Phase 3: Core Services

- [ ] Polymarket SDK wrapper service
- [ ] Opportunity detection service
- [ ] LangChain assessment chains
- [ ] Trade execution service

### Phase 4: API Endpoints

- [ ] Opportunities endpoints
- [ ] Leaderboard endpoints
- [ ] Trade execution endpoints
- [ ] Analytics endpoints

### Phase 5: Frontend Pages

- [ ] Opportunities page with AI scores
- [ ] Winners leaderboard
- [ ] Trade history and analytics
- [ ] Settings and configuration

### Phase 6: Advanced Features

- [ ] Automated strategy backtesting
- [ ] Portfolio rebalancing
- [ ] Risk management limits
- [ ] Notification system
- [ ] Mobile app

## Testing

```bash
# Backend tests
cd backend
uv run pytest

# Frontend tests
cd frontend
pnpm test
```

## Troubleshooting

### Backend won't start

- Check PostgreSQL is running: `psql -U polymarket_user -d polymarket -c "SELECT 1"`
- Check all dependencies installed: `uv sync`
- Check `.env` configuration is correct

### Frontend can't connect to backend

- Ensure backend is running on `http://localhost:8000`
- Verify `CORS_ALLOWED_ORIGINS` includes your frontend origin
- Check browser console for CORS errors
- Verify `VITE_API_URL` in `.env`

### Production security headers

- Keep reverse-proxy CSP aligned with the frontend build CSP (`connect-src` must include your API origin).
- Ensure cookie auth runs over HTTPS so `Secure` cookies are sent in production.

### Signature verification fails

- Ensure the connected wallet account is the one you intend to authenticate with
- Check wallet supports `personal_sign` (MetaMask, Rabby, WalletConnect wallets)
- If browser wallet is not detected, use WalletConnect QR flow
- If WalletConnect is unavailable, set `VITE_WALLETCONNECT_PROJECT_ID` in `frontend/.env`
- Try disconnecting/reconnecting the wallet and re-requesting a challenge

## License

MIT

## Support

For issues or questions, please open an issue on GitHub.
