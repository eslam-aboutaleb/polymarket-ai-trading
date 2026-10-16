"""Start and track process-owned background services."""

import asyncio
import logging

from app.utils.database import session_scope

logger = logging.getLogger(__name__)


async def _scheduler_watchdog() -> None:
    """Alert when a scheduler heartbeat becomes stale."""
    from app.utils.scheduler_lock import (
        SCHEDULER_INTERVALS,
        scheduler_heartbeat_age,
        scheduler_stale_threshold,
    )

    while True:
        await asyncio.sleep(60)
        for scheduler_name in SCHEDULER_INTERVALS:
            age = scheduler_heartbeat_age(scheduler_name)
            if age is None:
                continue
            threshold = scheduler_stale_threshold(scheduler_name)
            if age <= threshold:
                continue
            logger.error(
                "DEAD_MAN_SWITCH: scheduler '%s' heartbeat is %.0fs old (stale threshold %ds)",
                scheduler_name,
                age,
                threshold,
            )
            try:
                from app.services.users.alert_service import dispatch

                await dispatch(
                    "dead_man_switch",
                    None,
                    {
                        "scheduler": scheduler_name,
                        "heartbeat_age_seconds": round(age, 1),
                        "threshold_seconds": threshold,
                    },
                )
            except ImportError:
                pass
            except Exception:
                logger.warning(
                    "Dead-man alert dispatch failed for '%s'",
                    scheduler_name,
                    exc_info=True,
                )


async def start_background_services(
    background_tasks: list[asyncio.Task],
    *,
    session_factory=None,
) -> None:
    """Start scheduled services and retain tasks for graceful shutdown."""
    from app.repositories.notifications.followed_traders import NotificationFollowRepository
    from app.repositories.trading.follows import TradingFollowRepository
    from app.services.analytics.leaderboard_service import refresh_leaderboard_background
    from app.services.trading.trade_monitor import start_trade_monitor

    # Load currently-followed wallets so the monitor can subscribe.
    if session_factory is not None:
        db = session_factory()
        try:
            copy_wallets = TradingFollowRepository(db).list_active_wallets()
            notification_wallets = NotificationFollowRepository(db).list_active_wallets()
        finally:
            db.close()
    else:
        with session_scope() as db:
            copy_wallets = TradingFollowRepository(db).list_active_wallets()
            notification_wallets = NotificationFollowRepository(db).list_active_wallets()
    initial_wallets = sorted(
        {wallet.lower() for wallet in copy_wallets + notification_wallets if wallet}
    )

    if initial_wallets:
        from app.services.trading.trade_monitor import add_watched_wallet

        for wallet in initial_wallets:
            add_watched_wallet(wallet)

    monitor_task = asyncio.create_task(start_trade_monitor())
    background_tasks.append(monitor_task)

    # Start the CLOB WebSocket manager for real-time price ticks.
    from app.services.market_data.clob_ws_manager import start_clob_ws_manager

    start_clob_ws_manager()

    leaderboard_task = asyncio.create_task(refresh_leaderboard_background(db=None, interval=300))
    background_tasks.append(leaderboard_task)

    from app.services.trading.stop_loss_monitor import start_stop_loss_monitor

    await start_stop_loss_monitor()

    from app.services.trading.inverse_bot_monitor import start_inverse_bot_monitor

    await start_inverse_bot_monitor()

    from app.services.trading.market_maker_service import start_all_enabled_market_makers

    await start_all_enabled_market_makers()

    from app.services.trading.position_lifecycle_service import start_position_lifecycle_manager

    await start_position_lifecycle_manager()

    from app.services.trading.trade_aggregation_service import start_aggregation_service

    await start_aggregation_service()

    from app.services.analytics.arbitrage_service import start_arbitrage_monitor

    await start_arbitrage_monitor()

    from app.services.intelligence.news_service import start_news_generator

    await start_news_generator()

    from app.services.users.daily_report_service import start_daily_report_service

    await start_daily_report_service()

    from app.services.market_data.ctf_events_service import start_ctf_events_monitor

    await start_ctf_events_monitor()

    from app.services.trading.redemption_service import start_redemption_manager

    await start_redemption_manager()

    from app.services.trading.latency_arb_service import start_latency_arb_engine

    await start_latency_arb_engine()

    from app.services.analytics.equity_snapshot_service import start_equity_snapshot_service

    await start_equity_snapshot_service()

    from app.services.intelligence.forecast_resolution import start_forecast_resolution

    await start_forecast_resolution()

    from app.services.market_data.venue_market_service import start_venue_market_service

    await start_venue_market_service()

    from app.services.intelligence.model_drift_monitor import start_model_drift_monitor

    await start_model_drift_monitor()

    watchdog_task = asyncio.create_task(_scheduler_watchdog())
    background_tasks.append(watchdog_task)

    logger.info(
        "Background services started (trade monitor + leaderboard refresh + "
        "stop-loss monitor + inverse-bot monitor + market-maker + "
        "position-lifecycle + trade-aggregation + arbitrage-monitor + news-generator)"
    )
