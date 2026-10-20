"""Server-owned release barrier, applied before credentials or signing.

Legacy orders have no durable authorization and cannot prove reduce-only
semantics. A SELL flag, an enabled strategy or a user's simulation preference
is therefore insufficient authority to submit them, even during recovery.
"""

from app.config import get_settings


class LiveTradingBlocked(RuntimeError):
    """An order lacks permission to reach the live execution boundary."""


def block_legacy_live_execution(strategy: str) -> None:
    """Reject every legacy live path; configuration alone cannot release it."""
    settings = get_settings()
    if not settings.live_trading_enabled:
        raise LiveTradingBlocked("LIVE_TRADING_DISABLED")
    strategy_flags = {
        "copy": settings.live_copy_trading_enabled,
        "market_making": settings.live_market_making_enabled,
        "latency_arb": settings.live_latency_arb_enabled,
    }
    if strategy in strategy_flags and not strategy_flags[strategy]:
        raise LiveTradingBlocked("LIVE_STRATEGY_DISABLED")
    raise LiveTradingBlocked("DURABLE_EXECUTION_GATEWAY_REQUIRED")
