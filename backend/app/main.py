"""Main FastAPI application"""

import asyncio
import importlib
import logging.config
import os
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy import text

from app.api.routes import (
    analysis,
    auth,
    backtesting,
    binance_signals,
    credentials,
    debug,
    health,
    inverse_bot,
    latency_arb,
    market_maker,
    markets,
    news,
    notifications,
    portfolio,
    trades,
    whales,
)
from app.api.routes import settings as settings_routes
from app.config import get_settings
from app.middleware.request_logger import RequestLogMiddleware
from app.security.crypto import EncryptionConfigError, get_fernet_keyring
from app.security.rate_limit import limiter
from app.utils.database import SessionLocal, engine

logger = logging.getLogger(__name__)
settings = get_settings()

# Logging configuration. LOG_LEVEL is read from the environment so
# operators can raise verbosity without code changes; previously the
# root logger was never configured and INFO records from the trading
# paths were discarded.
logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
            },
        },
        "root": {
            "handlers": ["default"],
            "level": os.environ.get("LOG_LEVEL", "INFO").upper(),
        },
    }
)

# Rate limiter is constructed in app.security.rate_limit so route
# modules can reference it without importing app.main (circular).
# It is wired into the app below via app.state.limiter.


def _validate_startup_security() -> None:
    """Fail fast on security-critical misconfiguration."""
    # JWT strength is validated in Settings; this call keeps startup intent explicit.
    _ = settings.jwt_secret_key
    try:
        get_fernet_keyring()
    except EncryptionConfigError as exc:
        # Keep the API available in local/dev even if encrypted credential
        # storage isn't configured yet. Private-key login/trading paths still
        # fail closed via credential_store guards.
        if settings.is_local_environment:
            logger.warning(
                "Credential encryption not configured; private-key credential "
                "features are disabled until ENCRYPTION_MASTER_KEYS is set."
            )
            return
        raise RuntimeError(
            "Credential encryption is not configured. "
            "Set ENCRYPTION_MASTER_KEYS to one or more Fernet keys."
        ) from exc


def _validate_database_connectivity() -> None:
    """Fail fast when the application database is unavailable."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        logger.error("Database connectivity validation failed: %s", exc)
        raise RuntimeError("Database connection failed during startup.") from exc


def _sync_admin_wallets():
    """Promote / demote users based on ADMIN_WALLETS.

    If ADMIN_WALLETS is empty and ADMIN_SYNC_ALLOW_EMPTY=false, sync is skipped
    to avoid accidental mass demotion.
    """
    raw = settings.admin_wallets.strip()
    if not raw and not settings.admin_sync_allow_empty:
        logger.warning(
            "ADMIN_WALLETS is empty and ADMIN_SYNC_ALLOW_EMPTY=false; "
            "skipping admin sync to prevent destructive demotion."
        )
        return

    wallet_re = re.compile(r"^0x[a-fA-F0-9]{40}$")
    admin_set = set()
    for candidate in [a.strip() for a in raw.split(",") if a.strip()]:
        if not wallet_re.match(candidate):
            logger.warning("Ignoring invalid admin wallet in ADMIN_WALLETS: %s", candidate)
            continue
        admin_set.add(candidate.lower())

    db = SessionLocal()
    try:
        from app.models.audit_log import AdminAuditLog
        from app.models.user import User

        users = db.query(User).all()
        for user in users:
            should_be_admin = user.wallet_address.lower() in admin_set
            if user.is_admin != should_be_admin:
                previous_state = user.is_admin
                user.is_admin = should_be_admin
                db.add(
                    AdminAuditLog(
                        wallet_address=user.wallet_address,
                        action="promoted" if should_be_admin else "demoted",
                        source="env_sync",
                        previous_state=previous_state,
                        new_state=should_be_admin,
                    )
                )
                logger.warning(
                    "ADMIN_CHANGE: %s %s (was=%s now=%s)",
                    "PROMOTED" if should_be_admin else "DEMOTED",
                    user.wallet_address,
                    previous_state,
                    should_be_admin,
                )
        db.commit()
    except Exception as e:
        logger.error("Failed to sync admin wallets: %s", e)
        db.rollback()
        raise
    finally:
        db.close()


# Background task references
_background_tasks: list = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle for background services."""
    # ── Startup ──
    _validate_startup_security()
    _validate_database_connectivity()
    _sync_admin_wallets()

    try:
        from app.models.followed_trader import FollowedTrader
        from app.models.notification_followed_trader import NotificationFollowedTrader
        from app.services.leaderboard_service import refresh_leaderboard_background
        from app.services.trade_monitor import start_trade_monitor

        # Load currently-followed wallets so the monitor can subscribe
        db = SessionLocal()
        try:
            copy_wallets = (
                db.query(FollowedTrader.trader_wallet)
                .filter(
                    FollowedTrader.is_active,
                )
                .distinct()
                .all()
            )
            notif_wallets = (
                db.query(NotificationFollowedTrader.trader_wallet)
                .filter(
                    NotificationFollowedTrader.is_active,
                )
                .distinct()
                .all()
            )
            initial_wallets = list(
                {
                    w.trader_wallet.lower()
                    for w in (copy_wallets + notif_wallets)
                    if w and w.trader_wallet
                }
            )
        finally:
            db.close()

        if initial_wallets:
            from app.services.trade_monitor import add_watched_wallet

            for w in initial_wallets:
                add_watched_wallet(w)

        # Start background tasks
        monitor_task = asyncio.create_task(start_trade_monitor())
        _background_tasks.append(monitor_task)

        # Start the CLOB WebSocket manager for real-time price ticks
        from app.services.clob_ws_manager import start_clob_ws_manager

        start_clob_ws_manager()

        leaderboard_task = asyncio.create_task(
            refresh_leaderboard_background(db=None, interval=300)
        )
        _background_tasks.append(leaderboard_task)

        # Start stop-loss monitor
        from app.services.stop_loss_monitor import start_stop_loss_monitor

        await start_stop_loss_monitor()

        # Start inverse position bot monitor
        from app.services.inverse_bot_monitor import start_inverse_bot_monitor

        await start_inverse_bot_monitor()

        # Start market maker for all enabled configs
        from app.services.market_maker_service import start_all_enabled_market_makers

        await start_all_enabled_market_makers()

        # Start position lifecycle manager
        from app.services.position_lifecycle_service import start_position_lifecycle_manager

        await start_position_lifecycle_manager()

        # Start trade aggregation service
        from app.services.trade_aggregation_service import start_aggregation_service

        await start_aggregation_service()

        # Start arbitrage detection monitor
        from app.services.arbitrage_service import start_arbitrage_monitor

        await start_arbitrage_monitor()

        # Start news background generator
        from app.services.news_service import start_news_generator

        await start_news_generator()

        # Start on-chain whale monitor (CTF events)
        from app.services.ctf_events_service import start_ctf_events_monitor

        await start_ctf_events_monitor()

        # Start on-chain auto-redeem manager (dry-run by default)
        from app.services.redemption_service import start_redemption_manager

        await start_redemption_manager()

        # Start the latency-arbitrage engine (paper-first;
        # live mode behind LATENCY_ARB_LIVE)
        from app.services.latency_arb_service import (
            start_latency_arb_engine,
        )

        await start_latency_arb_engine()

        # Dead-man's switch watchdog: alerts when a scheduler
        # heartbeat goes stale (e.g. a monitor crashed without
        # cancelling its task).
        async def _scheduler_watchdog() -> None:
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
                        "DEAD_MAN_SWITCH: scheduler '%s' heartbeat is %.0fs old "
                        "(stale threshold %ds)",
                        scheduler_name,
                        age,
                        threshold,
                    )
                    try:
                        from app.services.alert_service import dispatch

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

        watchdog_task = asyncio.create_task(_scheduler_watchdog())
        _background_tasks.append(watchdog_task)

        logger.info(
            "Background services started (trade monitor + leaderboard refresh + "
            "stop-loss monitor + inverse-bot monitor + market-maker + "
            "position-lifecycle + trade-aggregation + arbitrage-monitor + news-generator)"
        )
    except Exception as e:
        logger.warning(f"Background services failed to start: {e}")

    yield

    # ── Shutdown ──
    # Each stopper is independent: one failing must not prevent the rest from
    # releasing their resources, so failures are logged and shutdown continues.
    shutdown_hooks = [
        ("trade_monitor", "app.services.trade_monitor", "stop_trade_monitor"),
        (
            "clob_ws_manager",
            "app.services.clob_ws_manager",
            "stop_clob_ws_manager",
        ),
        ("stop_loss_monitor", "app.services.stop_loss_monitor", "stop_stop_loss_monitor"),
        ("inverse_bot_monitor", "app.services.inverse_bot_monitor", "stop_inverse_bot_monitor"),
        ("market_maker_service", "app.services.market_maker_service", "stop_all_market_makers"),
        (
            "position_lifecycle_service",
            "app.services.position_lifecycle_service",
            "stop_position_lifecycle_manager",
        ),
        (
            "trade_aggregation_service",
            "app.services.trade_aggregation_service",
            "stop_aggregation_service",
        ),
        ("arbitrage_service", "app.services.arbitrage_service", "stop_arbitrage_monitor"),
        ("news_service", "app.services.news_service", "stop_news_generator"),
        ("ctf_events_service", "app.services.ctf_events_service", "stop_ctf_events_monitor"),
        (
            "redemption_service",
            "app.services.redemption_service",
            "stop_redemption_manager",
        ),
        (
            "latency_arb_service",
            "app.services.latency_arb_service",
            "stop_latency_arb_engine",
        ),
        ("analysis_client", "app.grpc_clients.analysis_client", "close_shared_channels"),
    ]
    for label, module_path, attr in shutdown_hooks:
        try:
            module = importlib.import_module(module_path)
            await getattr(module, attr)()
        except Exception:
            logger.warning(
                "Shutdown hook %s (%s.%s) failed; continuing",
                label,
                module_path,
                attr,
                exc_info=True,
            )
    for task in _background_tasks:
        task.cancel()
    logger.info("Background services stopped")


# Create FastAPI app
app = FastAPI(
    title=settings.api_title,
    description=settings.api_description,
    version=settings.api_version,
    lifespan=lifespan,
)

# Add rate limiter to app state
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Add baseline browser security headers for API responses.
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)

    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=()",
    )

    docs_paths = {"/docs", "/redoc", "/openapi.json"}
    is_docs_path = request.url.path in docs_paths or request.url.path.startswith("/docs/")
    if not is_docs_path:
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )

    return response


# Add request logging middleware (outermost — captures everything incl. CORS)
app.add_middleware(RequestLogMiddleware)


# Include routers
app.include_router(auth.router)
app.include_router(credentials.router)
app.include_router(analysis.router)
app.include_router(portfolio.router)
app.include_router(settings_routes.router)
app.include_router(trades.router)
app.include_router(markets.router)
if settings.debug_endpoints_active:
    app.include_router(debug.router)
else:
    logger.info("Debug router disabled (DEBUG_ENDPOINTS_ENABLED=false).")
app.include_router(inverse_bot.router)
app.include_router(market_maker.router)
app.include_router(backtesting.router)
app.include_router(binance_signals.router)
app.include_router(news.router)
app.include_router(health.router)
app.include_router(whales.router)
app.include_router(notifications.router)
app.include_router(latency_arb.router)


# Root endpoint
@app.get("/")
async def root():
    """Root endpoint"""
    return {"message": "Welcome to Polymarket AI Trading API", "docs": "/docs", "redoc": "/redoc"}


# Error handlers
@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle general exceptions"""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    detail = str(exc) if settings.is_local_environment else "Internal server error"
    return JSONResponse(status_code=500, content={"detail": detail})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.reload)
