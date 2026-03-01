# Quick Start Guide

## 🚀 Getting Started in 5 Minutes

### Option 1: Using the Startup Script (Recommended)

```bash
cd /Users/eslamaboutaleb/Documents/polymarket

# Make script executable (first time only)
chmod +x start.sh

# Configure your environment
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env

# Edit backend/.env with your API keys:
# - JWT_SECRET_KEY (generate a random string)
# - POLYMARKET_PRIVATE_KEY (your wallet private key)
# - OPENAI_API_KEY (your OpenAI key)

# Start everything
./start.sh
```

### Option 2: Manual Start

**Terminal 1 - Backend:**

```bash
cd /Users/eslamaboutaleb/Documents/polymarket/backend
cp .env.example .env
# Edit .env with your settings
uv sync
uv run uvicorn app.main:app --reload
```

**Terminal 2 - Frontend:**

```bash
cd /Users/eslamaboutaleb/Documents/polymarket/frontend
cp .env.example .env
pnpm install
pnpm dev
```

**Terminal 3 - Database (if not using Docker):**

```bash
# Make sure PostgreSQL is running
# macOS with brew: brew services start postgresql
# Docker: docker-compose up postgres
```

### Option 3: Docker Compose

```bash
cd /Users/eslamaboutaleb/Documents/polymarket

# Create .env files
cp backend/.env.example backend/.env
# Edit backend/.env with your keys

# Start all services
docker-compose up
```

## 🌐 Access Points

Once everything is running:

- **Frontend**: http://localhost:5173
- **Backend API**: http://localhost:8000
- **API Documentation**: http://localhost:8000/docs
- **Database**: localhost:5432 (PostgreSQL)

## 🔐 Testing the Login Flow

1. **Visit Frontend**: Go to http://localhost:5173
2. **Enter Wallet Address**: Use a valid Ethereum address (0x...)
3. **Sign Message**: MetaMask will ask you to sign (or use any Web3 wallet)
4. **Select Option**: Check "Keep me logged in" for 90 days, or leave unchecked for 7 days
5. **Submit**: Click "Sign Message" to complete authentication

## 📝 Configuration

### Backend Environment Variables

```env
# Database
DATABASE_URL=postgresql://polymarket_user:polymarket_password@localhost:5432/polymarket

# JWT Tokens
JWT_SECRET_KEY=your-super-secret-change-in-production
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7

# Polymarket
POLYMARKET_PRIVATE_KEY=your_wallet_private_key
POLYMARKET_CHAIN_ID=137  # Polygon Mainnet

# OpenAI
OPENAI_API_KEY=sk-your-openai-key

# Trading Settings
EXECUTION_MODE=approval  # or 'auto'
MAX_POSITION_SIZE=1000.0  # Max USDC per trade
```

### Frontend Environment Variables

```env
VITE_API_URL=http://localhost:8000
VITE_WALLETCONNECT_PROJECT_ID=  # Required for WalletConnect QR login
```

## 📚 Project Structure

```
polymarket/
├── backend/              # FastAPI Python backend
│   ├── app/
│   │   ├── models/       # Database models (SQLAlchemy)
│   │   ├── api/routes/   # API endpoints
│   │   ├── security/     # JWT & auth
│   │   ├── services/     # Business logic (to be implemented)
│   │   ├── llm/          # LangChain chains (to be implemented)
│   │   └── main.py       # FastAPI app
│   ├── .env.example      # Environment template
│   └── pyproject.toml    # UV dependencies
│
├── frontend/             # React TypeScript frontend
│   ├── src/
│   │   ├── pages/        # LoginPage, DashboardPage
│   │   ├── components/   # LoginForm, Navigation, Dashboard
│   │   ├── services/     # API client, auth service
│   │   ├── store/        # Zustand auth store
│   │   └── App.tsx       # Main component
│   ├── .env.example      # Environment template
│   └── package.json      # pnpm dependencies
│
├── docker-compose.yml    # Docker services (PostgreSQL, Backend, Frontend)
├── start.sh              # Startup script
└── README.md             # Full documentation
```

## 🔄 Authentication Flow

```
User Connects Wallet
    ↓
Frontend requests challenge from /api/auth/login
    ↓
Backend returns challenge message + timestamp
    ↓
User signs message with Browser Wallet or WalletConnect wallet
    ↓
Frontend sends signature to /api/auth/verify
    ↓
Backend verifies signature & recovers wallet address
    ↓
Backend creates/updates User in database
    ↓
Backend generates:
  - access_token (JWT, 15 min expiry)
  - refresh_token (7 or 90 days based on "Keep me logged in")
    ↓
Frontend stores tokens in Zustand state + localStorage
    ↓
Frontend automatically includes access_token in Authorization header
    ↓
Before token expires: Auto-refresh using refresh_token
    ↓
On logout: Revoke refresh_token in database
```

## 🛠️ Development Commands

### Backend

```bash
cd backend

# Install/sync dependencies
uv sync

# Run development server (with auto-reload)
uv run uvicorn app.main:app --reload

# Run tests
uv run pytest

# Create database migrations
uv run alembic revision --autogenerate -m "description"
uv run alembic upgrade head
```

### Frontend

```bash
cd frontend

# Install dependencies
pnpm install

# Start dev server (with hot reload)
pnpm dev

# Build for production
pnpm build

# Preview production build
pnpm preview

# Type checking
pnpm type-check

# Linting
pnpm lint
```

## 🐛 Troubleshooting

### Backend won't start

- Check PostgreSQL is running: `psql -U polymarket_user -d polymarket -c "SELECT 1"`
- Verify `.env` file exists and has correct DATABASE_URL
- Check all dependencies are installed: `uv sync`
- Clear Python cache: `find . -type d -name __pycache__ -exec rm -r {} +` (ignore errors)

### Frontend shows "Cannot reach backend"

- Make sure backend is running on port 8000
- Check CORS is enabled (it is by default)
- Verify `VITE_API_URL` in `.env` is correct
- Check browser console for CORS/network errors (F12)

### Signature verification fails

- Ensure you're using the same wallet address for both address input and signing
- Try disconnecting and reconnecting MetaMask
- Check browser wallet extension has access to the site

### Database migrations needed

```bash
cd backend
alembic upgrade head
```

### Reset everything

```bash
# Stop all services
Ctrl+C

# Remove dependencies and cache
rm -rf backend/.venv frontend/node_modules
rm -rf backend/.uv

# Clean PostgreSQL (if using Docker)
docker-compose down -v

# Restart
./start.sh
```

## 📚 Next Steps

1. **Test Login**: Verify authentication flow works end-to-end
2. **Set Up Polymarket API**: Connect py-clob-client to fetch market data
3. **Implement Opportunity Detection**: Create service to identify winning traders
4. **Build LangChain Chains**: Create assessment chains using OpenAI
5. **Create Additional API Endpoints**: Opportunities, trades, leaderboard, etc.
6. **Build Dashboard Pages**: Implement frontend pages for each feature

## 🤝 Support

- **API Documentation**: http://localhost:8000/docs
- **FastAPI Docs**: https://fastapi.tiangolo.com
- **LangChain Docs**: https://python.langchain.com
- **Polymarket CLI Docs**: Check your installed polymarket CLI with `polymarket --help`
- **React Docs**: https://react.dev
- **Zustand Docs**: https://zustand-demo.vercel.app

## 🚢 Deployment

See [README.md](README.md) for detailed deployment instructions with Docker and cloud services.
