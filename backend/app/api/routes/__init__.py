"""HTTP route layer.

Route modules are grouped by bounded context (``auth``, ``markets``,
``trading``, ``portfolio``, ``analysis``, ``billing``, ``platform``). Each
domain package owns its route modules and exposes FastAPI ``APIRouter``
objects; :func:`register_routers` is the single wiring point the
application factory uses to mount the HTTP surface.

Imports are deliberately lazy: importing one route module (directly or as
part of the app) must not pull in the entire route graph, which would make
``app.api.deps`` (imported by nearly every route) part of an import cycle.
:func:`register_routers` is the only place that loads every domain, and it
runs once at application start-up.

Adding an endpoint means adding (or editing) a module inside its domain
package and, when it is a new router, adding one line to the sequence in
:func:`register_routers` — no edit to ``app.main`` is required.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

__all__ = ["register_routers"]

logger = logging.getLogger(__name__)


def register_routers(app: FastAPI, *, debug_enabled: bool = True) -> None:
    """Mount every domain router, preserving the original mount order.

    Mount order matters when two routers could match the same path, so the
    order below is the order the routes were historically registered in.
    The debug router is the only feature-gated router and stays in its
    original position in the sequence.
    """
    from app.api.routes.analysis import analysis, calibration, experiments, orchestration
    from app.api.routes.auth import accounts, auth, credentials
    from app.api.routes.billing import billing
    from app.api.routes.markets import (
        binance_signals,
        markets,
        microstructure,
        news,
        observations,
        venues,
        whales,
    )
    from app.api.routes.platform import (
        collaboration,
        debug,
        external,
        health,
        notifications,
        ws_v1,
    )
    from app.api.routes.platform import settings as settings_routes
    from app.api.routes.portfolio import portfolio, risk_report
    from app.api.routes.trading import (
        backtesting,
        follower_replay,
        inverse_bot,
        latency_arb,
        market_maker,
        paper,
        replay_evidence,
        risk,
        trades,
    )

    for module in (
        accounts,
        auth,
        credentials,
        analysis,
        calibration,
        collaboration,
        experiments,
        portfolio,
        risk_report,
        risk,
        settings_routes,
        trades,
        markets,
        observations,
        microstructure,
    ):
        app.include_router(module.router)

    if debug_enabled:
        app.include_router(debug.router)
    else:
        logger.info("Debug router disabled (DEBUG_ENDPOINTS_ENABLED=false).")

    for module in (
        inverse_bot,
        market_maker,
        replay_evidence,
        paper,
        backtesting,
        follower_replay,
        binance_signals,
        news,
        health,
        whales,
        notifications,
        latency_arb,
        orchestration,
        venues,
        billing,
    ):
        app.include_router(module.router)

    # The external API module mounts four routers: /api/v1/external/*
    # (key-authenticated), /api/settings/api-keys/* (browser session),
    # /api/portfolio/exposure (user-authenticated), and /ws/v1.
    app.include_router(external.router)
    app.include_router(trades.safety_router)
    app.include_router(external.api_keys_router)
    app.include_router(external.exposure_router)
    app.include_router(ws_v1.router)
