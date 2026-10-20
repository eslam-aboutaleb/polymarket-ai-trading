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
    """Unimplemented CLOB adapter kept as an explicit extension point.

    It deliberately fails closed. Returning fabricated order IDs, fills, or
    cancellation results would make callers believe a live order was handled
    when no exchange operation took place. Register a complete adapter with
    ``execution_registry`` before enabling live execution.
    """

    mode = ExecutionMode.LIVE

    def __init__(self, clob_client: Any = None) -> None:
        self.clob_client = clob_client

    async def place_order(
        self,
        request: OrderPlacementRequest,  # noqa: ARG002 - protocol signature
    ) -> OrderPlacementResult:
        raise ExecutionEngineUnavailable(
            "Live CLOB order placement is not implemented; register a complete live adapter."
        )

    async def cancel_order(self, order_id: str, **kwargs: Any) -> bool:  # noqa: ARG002
        raise ExecutionEngineUnavailable(
            "Live CLOB cancellation is not implemented; register a complete live adapter."
        )

    async def get_midpoint(self, token_id: str) -> float | None:  # noqa: ARG002
        raise ExecutionEngineUnavailable(
            "Live CLOB midpoint lookup is not implemented; register a complete live adapter."
        )


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
            from app.services.trading.live_trading_policy import block_legacy_live_execution

            block_legacy_live_execution("manual")
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


def get_execution_engine(mode: str | ExecutionMode, **kwargs: Any) -> ExecutionEngine:
    """Resolve an execution engine by mode.

    Requires an explicit named mode. Booleans cannot authorize live execution.
    """
    selected_mode = ExecutionMode(str(mode).lower())
    return execution_registry.get(selected_mode, **kwargs)
