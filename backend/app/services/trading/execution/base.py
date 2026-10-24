"""Pluggable Execution Engine Gateway and Adapters (SOLID / Strategy pattern).

Separates the trading execution strategy (Live CLOB vs. Paper vs. Backtest/Sandbox)
behind a unified ``ExecutionEngine`` protocol, eliminating hardcoded `if is_paper:`
conditionals across trading monitors and services.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class ExecutionEngineUnavailable(RuntimeError):
    """Raised when a requested execution mode has no configured adapter."""


class ExecutionMode(StrEnum):
    """Trading execution modes."""

    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"
    SANDBOX = "sandbox"


@dataclass
class OrderPlacementRequest:
    """Request payload for placing an order through any execution engine."""

    user_id: int
    token_id: str
    price: float
    size: float
    side: str  # "BUY" or "SELL"
    order_type: str = "GTC"
    account_id: int | None = None
    strategy: str = "manual"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderPlacementResult:
    """Standardized response from placing an order."""

    order_id: str
    status: str  # "filled", "open", "simulated", "rejected"
    executed_price: float
    executed_size: float
    is_simulated: bool
    fee: float = 0.0
    raw_response: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None


@runtime_checkable
class ExecutionEngine(Protocol):
    """Protocol that every execution engine (Live, Paper, Backtest) satisfies."""

    mode: ExecutionMode

    async def place_order(self, request: OrderPlacementRequest) -> OrderPlacementResult:
        """Place an order through this engine."""
        ...

    async def cancel_order(self, order_id: str, **kwargs: Any) -> bool:
        """Cancel an open order."""
        ...

    async def get_midpoint(self, token_id: str) -> float | None:
        """Fetch current midpoint price for a token."""
        ...


class PaperExecutionEngine:
    """Simulated execution engine for paper trading without private keys or gas."""

    mode = ExecutionMode.PAPER

    def __init__(self, slippage_bps: int = 10) -> None:
        self.slippage_bps = slippage_bps

    async def place_order(self, request: OrderPlacementRequest) -> OrderPlacementResult:
        # Apply simulated slippage
        slippage_factor = 1.0 + (
            (self.slippage_bps / 10000.0)
            if request.side.upper() == "BUY"
            else (-self.slippage_bps / 10000.0)
        )
        simulated_price = round(request.price * slippage_factor, 4)
        order_id = f"sim-{request.token_id[:8]}-{request.user_id}"

        return OrderPlacementResult(
            order_id=order_id,
            status="simulated",
            executed_price=simulated_price,
            executed_size=request.size,
            is_simulated=True,
            raw_response={"mode": "paper", "simulated_slippage_bps": self.slippage_bps},
        )

    async def cancel_order(self, order_id: str, **kwargs: Any) -> bool:  # noqa: ARG002
        return True

    async def get_midpoint(self, token_id: str) -> float | None:  # noqa: ARG002
        return None


class ClobExecutionEngine:
    """Real exchange execution through durable account authorization."""

    mode = ExecutionMode.LIVE

    def __init__(self, db=None, user_id: int | None = None) -> None:
        if db is None or user_id is None:
            raise ExecutionEngineUnavailable("A database session and account owner are required")
        self.db = db
        self.user_id = user_id

    async def place_order(self, request: OrderPlacementRequest) -> OrderPlacementResult:
        from app.services.trading.execution.async_io import run_blocking_execution
        from app.services.trading.execution.live import place_live_order

        if request.user_id != self.user_id:
            raise ValueError("Execution engine owner mismatch")
        result = await run_blocking_execution(
            place_live_order,
            self.db,
            user_id=self.user_id,
            token_id=request.token_id,
            side=request.side,
            price=request.price,
            size=request.size,
            strategy=request.strategy,
            account_id=request.account_id,
            order_type=request.order_type,
            **request.metadata,
        )
        return OrderPlacementResult(
            order_id=result.get("order_hash") or "",
            status=result["status"],
            executed_price=0,
            executed_size=0,
            is_simulated=False,
            raw_response=result,
            error_message=result.get("error"),
        )

    async def cancel_order(self, order_id: str, **kwargs: Any) -> bool:  # noqa: ARG002
        from app.services.trading.execution.async_io import run_blocking_execution
        from app.services.trading.execution.live import reconcile_order

        result = await run_blocking_execution(
            reconcile_order, self.db, user_id=self.user_id, order_hash=order_id, cancel=True
        )
        return result["status"] == "cancelled"

    async def get_midpoint(self, token_id: str) -> float | None:
        import asyncio

        from py_clob_client_v2 import ClobClient

        from app.services.market_data.polymarket_service import POLYMARKET_CLOB_API

        client = ClobClient(host=POLYMARKET_CLOB_API, chain_id=137)
        response = await asyncio.to_thread(client.get_midpoint, token_id)
        value = float(response["mid"])
        if not 0 < value < 1:
            raise ValueError("Invalid exchange midpoint")
        return value


class ExecutionEngineRegistry:
    """Thread-safe registry for execution engines."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., ExecutionEngine]] = {}
        self._lock = threading.Lock()

    def register(self, mode: str | ExecutionMode, factory: Callable[..., ExecutionEngine]) -> None:
        key = str(mode).lower()
        with self._lock:
            self._factories[key] = factory

    def get(self, mode: str | ExecutionMode, **kwargs: Any) -> ExecutionEngine:
        key = str(mode).lower()
        if key == ExecutionMode.LIVE:
            from app.services.trading.execution.live import require_enabled
            from app.services.trading.live_trading_policy import LiveTradingBlocked

            require_enabled("manual")
            if kwargs.get("db") is None or kwargs.get("user_id") is None:
                raise LiveTradingBlocked("DURABLE_EXECUTION_CONTEXT_REQUIRED")
        with self._lock:
            factory = self._factories.get(key)
        if factory is None:
            if key == ExecutionMode.PAPER:
                return PaperExecutionEngine(**kwargs)
            if key == ExecutionMode.LIVE:
                raise ExecutionEngineUnavailable("No live execution adapter is registered.")
            raise ValueError(f"Unknown execution mode: {mode}")
        return factory(**kwargs)


execution_registry = ExecutionEngineRegistry()
execution_registry.register(ExecutionMode.PAPER, lambda **kw: PaperExecutionEngine(**kw))
execution_registry.register(ExecutionMode.LIVE, lambda **kw: ClobExecutionEngine(**kw))


def get_execution_engine(mode: str | ExecutionMode, **kwargs: Any) -> ExecutionEngine:
    """Resolve an execution engine by mode.

    Requires an explicit named mode. Booleans cannot authorize live execution.
    """
    selected_mode = ExecutionMode(str(mode).lower())
    return execution_registry.get(selected_mode, **kwargs)
