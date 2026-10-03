"""Backtesting API routes."""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.models.backtest_run import BacktestRun
from app.services.backtesting_service import (
    create_and_run_backtest,
    get_backtest_run,
    get_backtest_runs,
)
from app.utils.database import get_db

router = APIRouter(prefix="/api/backtesting", tags=["backtesting"])


# ── Pydantic schemas ──


class BacktestRequest(BaseModel):
    strategy_type: str = Field(..., pattern="^(copy_trade|indicator|custom)$")
    strategy_name: str = Field(default="Unnamed Strategy")
    start_date: str = Field(..., min_length=8)  # e.g. "2024-01-01"
    end_date: str = Field(..., min_length=8)
    parameters: dict = Field(default_factory=dict)


class BacktestRunResponse(BaseModel):
    id: int
    strategy_type: str
    strategy_name: str
    start_date: str
    end_date: str
    parameters: dict
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float | None
    total_pnl: float
    max_drawdown: float | None
    sharpe_ratio: float | None
    profit_factor: float | None
    avg_trade_pnl: float | None
    max_consecutive_losses: int | None
    total_volume: float | None
    status: str
    error_message: str | None
    trade_log: list | None
    indicator_values: dict | None
    created_at: str
    completed_at: str | None


class BacktestListResponse(BaseModel):
    runs: list[BacktestRunResponse]
    total: int


# ── Helpers ──


def _to_response(run: BacktestRun) -> BacktestRunResponse:
    return BacktestRunResponse(
        id=run.id,
        strategy_type=run.strategy_type,
        strategy_name=run.strategy_name or "",
        start_date=run.start_date,
        end_date=run.end_date,
        parameters=run.parameters or {},
        total_trades=run.total_trades or 0,
        winning_trades=run.winning_trades or 0,
        losing_trades=run.losing_trades or 0,
        win_rate=run.win_rate,
        total_pnl=run.total_pnl or 0.0,
        max_drawdown=run.max_drawdown,
        sharpe_ratio=run.sharpe_ratio,
        profit_factor=run.profit_factor,
        avg_trade_pnl=run.avg_trade_pnl,
        max_consecutive_losses=run.max_consecutive_losses,
        total_volume=run.total_volume,
        status=run.status or "pending",
        error_message=run.error_message,
        trade_log=run.trade_log,
        indicator_values=run.indicator_values,
        created_at=run.created_at.isoformat() if run.created_at else "",
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
    )


# ── Endpoints ──


@router.get("/runs", response_model=BacktestListResponse)
async def list_backtest_runs(
    limit: int = 20,
    current_user: dict = Depends(get_current_user_from_token),
):
    """List recent backtest runs."""
    user_id = current_user.get("user_id")
    runs = get_backtest_runs(user_id, limit=min(limit, 100))
    return BacktestListResponse(
        runs=[_to_response(r) for r in runs],
        total=len(runs),
    )


@router.get("/runs/{run_id}", response_model=BacktestRunResponse)
async def get_backtest_run_endpoint(
    run_id: int,
    current_user: dict = Depends(get_current_user_from_token),
):
    """Get a specific backtest run with full details."""
    user_id = current_user.get("user_id")
    run = get_backtest_run(run_id, user_id)
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return _to_response(run)


@router.post("/runs", response_model=BacktestRunResponse)
async def create_backtest_run(
    body: BacktestRequest,
    current_user: dict = Depends(get_current_user_from_token),
):
    """Create and execute a new backtest run."""
    user_id = current_user.get("user_id")

    try:
        run = await create_and_run_backtest(
            user_id=user_id,
            strategy_type=body.strategy_type,
            strategy_name=body.strategy_name,
            start_date=body.start_date,
            end_date=body.end_date,
            parameters=body.parameters,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if not run:
        raise HTTPException(status_code=500, detail="Failed to create backtest run")
    return _to_response(run)


@router.delete("/runs/{run_id}")
async def delete_backtest_run(
    run_id: int,
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Delete a backtest run."""
    user_id = current_user.get("user_id")
    run = (
        db.query(BacktestRun)
        .filter(
            BacktestRun.id == run_id,
            BacktestRun.user_id == user_id,
        )
        .first()
    )
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found")

    db.delete(run)
    db.commit()
    return {"status": "deleted", "id": run_id}


@router.get("/strategies")
async def list_available_strategies(
    current_user: dict = Depends(get_current_user_from_token),
):
    """List available backtesting strategies."""
    return {
        "strategies": [
            {
                "type": "copy_trade",
                "name": "Copy Trade Replay",
                "description": "Replay historical trades from a followed trader",
                "parameters": {
                    "followed_wallet": "Wallet address to simulate copying",
                    "max_position_size": "Max USDC per position (default: 100)",
                    "daily_loss_limit": "Max daily loss before stopping (default: 50)",
                },
            },
            {
                "type": "indicator",
                "name": "Technical Indicator Strategy",
                "description": "Trade based on RSI, MACD, or Bollinger Bands signals",
                "parameters": {
                    "condition_id": "Market condition ID to backtest",
                    "strategy": "rsi_mean_reversion | macd_crossover | bollinger_bounce",
                    "position_size": "USDC per trade (default: 10)",
                    "rsi_oversold": "RSI buy threshold (default: 30)",
                    "rsi_overbought": "RSI sell threshold (default: 70)",
                },
            },
        ]
    }
