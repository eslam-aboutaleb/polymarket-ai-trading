"""Coordinate application startup, process roles, and graceful shutdown."""

import contextlib
import importlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from app.config import Settings

logger = logging.getLogger(__name__)

ShutdownHook = tuple[str, str, str]

SHUTDOWN_HOOKS: tuple[ShutdownHook, ...] = (
    ("trade_monitor", "app.services.trading.trade_monitor", "stop_trade_monitor"),
    ("clob_ws_manager", "app.services.market_data.clob_ws_manager", "stop_clob_ws_manager"),
    ("stop_loss_monitor", "app.services.trading.stop_loss_monitor", "stop_stop_loss_monitor"),
    (
        "inverse_bot_monitor",
        "app.services.trading.inverse_bot_monitor",
        "stop_inverse_bot_monitor",
    ),
    (
        "market_maker_service",
        "app.services.trading.market_maker_service",
        "stop_all_market_makers",
    ),
    (
        "position_lifecycle_service",
        "app.services.trading.position_lifecycle_service",
        "stop_position_lifecycle_manager",
    ),
    (
        "trade_aggregation_service",
        "app.services.trading.trade_aggregation_service",
        "stop_aggregation_service",
    ),
    ("arbitrage_service", "app.services.analytics.arbitrage_service", "stop_arbitrage_monitor"),
    ("news_service", "app.services.intelligence.news_service", "stop_news_generator"),
    (
        "ctf_events_service",
        "app.services.market_data.ctf_events_service",
        "stop_ctf_events_monitor",
    ),
    (
        "redemption_service",
        "app.services.trading.redemption_service",
        "stop_redemption_manager",
    ),
    (
        "latency_arb_service",
        "app.services.trading.latency_arb_service",
        "stop_latency_arb_engine",
    ),
    (
        "daily_report_service",
        "app.services.users.daily_report_service",
        "stop_daily_report_service",
    ),
    (
        "equity_snapshot_service",
        "app.services.analytics.equity_snapshot_service",
        "stop_equity_snapshot_service",
    ),
    (
        "forecast_resolution",
        "app.services.intelligence.forecast_resolution",
        "stop_forecast_resolution",
    ),
    (
        "model_drift_monitor",
        "app.services.intelligence.model_drift_monitor",
        "stop_model_drift_monitor",
    ),
    ("analysis_client", "app.grpc_clients.analysis_client", "close_shared_channels"),
)


async def _stop_background_services(
    settings: Settings,
    background_tasks: list[Any],
) -> None:
    """Run each service stopper independently, then cancel owned tasks."""
    for label, module_path, attribute in SHUTDOWN_HOOKS:
        try:
            module = importlib.import_module(module_path)
            await getattr(module, attribute)()
        except Exception:
            logger.warning(
                "Shutdown hook %s (%s.%s) failed; continuing",
                label,
                module_path,
                attribute,
                exc_info=True,
            )

    for task in background_tasks:
        task.cancel()

    if settings.runs_background_schedulers():
        # Drain queued events so a graceful shutdown does not lose the final writes.
        with contextlib.suppress(Exception):
            from app.services.platform.event_log import stop_event_writer

            await stop_event_writer()
    logger.info("Background services stopped")


@asynccontextmanager
async def application_lifespan(
    app: FastAPI,
    *,
    settings: Settings,
    background_tasks: list[Any],
    validate_startup_security: Callable[[], None],
    validate_database_connectivity: Callable[[], None],
    sync_admin_wallets: Callable[[], None],
    start_background_services: Callable[[], Awaitable[None]],
):
    """Run startup hooks, optional worker services, and graceful shutdown."""
    del app  # The current lifecycle does not need request-application state.
    validate_startup_security()
    validate_database_connectivity()

    # Register configured adapters at the composition boundary so deployments
    # can replace them without import-order side effects.
    from app.services.market_data.venue_market_service import configure_builtin_adapters

    configure_builtin_adapters()
    sync_admin_wallets()

    if settings.runs_background_schedulers():
        try:
            from app.services.platform.event_log import start_event_writer

            start_event_writer()
            await start_background_services()
        except Exception as exc:
            logger.warning("Background services failed to start: %s", exc)
    else:
        logger.info(
            "ROLE=%s: background schedulers are owned by a worker process; serving HTTP only",
            settings.role,
        )

    yield

    await _stop_background_services(settings, background_tasks)
