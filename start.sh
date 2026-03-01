#!/bin/bash

# Polymarket Development Startup Script

echo "🚀 Starting Polymarket AI Trading Platform..."
echo ""

# Check for required commands
check_command() {
    if ! command -v $1 &> /dev/null; then
        echo "❌ Error: $1 is not installed"
        exit 1
    fi
}

# Check PostgreSQL is running
echo "📊 Checking PostgreSQL..."
if ! psql -U polymarket_user -d polymarket -c "SELECT 1" &> /dev/null; then
    echo "⚠️  PostgreSQL not found. Starting Docker containers..."
    docker-compose up -d postgres
    echo "⏳ Waiting for PostgreSQL to be ready..."
    sleep 5
fi
echo "✅ PostgreSQL ready"

# Backend setup
echo ""
echo "⚙️  Setting up backend..."
cd backend

# Check .env exists
if [ ! -f .env ]; then
    echo "📝 Creating .env from template..."
    cp .env.example .env
    echo "⚠️  Please edit backend/.env with your settings!"
fi

# Install dependencies
echo "📦 Installing Python dependencies..."
uv sync

# Initialize database
echo "🗄️  Initializing database..."
# Database tables will be created automatically when app starts

echo ""
echo "🎯 Starting backend server..."
uv run uvicorn app.main:app --reload --port 8000 &
BACKEND_PID=$!
echo "✅ Backend started (PID: $BACKEND_PID) at http://localhost:8000"

# Frontend setup
echo ""
echo "⚙️  Setting up frontend..."
cd ../frontend

# Check .env exists
if [ ! -f .env ]; then
    echo "📝 Creating .env from template..."
    cp .env.example .env
fi

# Install dependencies
echo "📦 Installing Node dependencies..."
pnpm install

echo ""
echo "🎯 Starting frontend server..."
pnpm dev &
FRONTEND_PID=$!
echo "✅ Frontend started (PID: $FRONTEND_PID) at http://localhost:5173"

echo ""
echo "═════════════════════════════════════════════════════════"
echo "🎉 Polymarket AI Trading Platform is running!"
echo ""
echo "📍 Frontend: http://localhost:5173"
echo "📍 Backend: http://localhost:8000"
echo "📍 API Docs: http://localhost:8000/docs"
echo "📍 Database: localhost:5432"
echo ""
echo "⚠️  To stop all services, press Ctrl+C"
echo "═════════════════════════════════════════════════════════"
echo ""

# Wait for background processes
wait
