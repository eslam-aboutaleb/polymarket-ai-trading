"""Pluggable Execution Engine Package.

Exposes ExecutionEngine protocol, ExecutionMode, PaperExecutionEngine,
ClobExecutionEngine, and get_execution_engine factory.
"""

from app.services.trading.execution.base import (
    ClobExecutionEngine,
    ExecutionEngine,
    ExecutionEngineRegistry,
    ExecutionEngineUnavailable,
    ExecutionMode,
    OrderPlacementRequest,
    OrderPlacementResult,
    PaperExecutionEngine,
    execution_registry,
    get_execution_engine,
)

__all__ = [
    "ClobExecutionEngine",
    "ExecutionEngine",
    "ExecutionEngineRegistry",
    "ExecutionEngineUnavailable",
    "ExecutionMode",
    "OrderPlacementRequest",
    "OrderPlacementResult",
    "PaperExecutionEngine",
    "execution_registry",
    "get_execution_engine",
]
