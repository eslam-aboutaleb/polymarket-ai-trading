"""Health and metrics endpoints.

GET /health — real liveness probe: database, cache backend, gRPC AI
backends and scheduler heartbeats. Returns 503 when a critical
dependency (the database) is down so load balancers and orchestrators
stop routing traffic; gRPC or stale-scheduler conditions report
"degraded" with HTTP 200.

GET /metrics — Prometheus text metrics, admin only.
"""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.models.user import User
from app.utils.database import engine, get_db
from app.utils.metrics import gauge, render_prometheus
from app.utils.scheduler_lock import (
    SCHEDULER_INTERVALS,
    scheduler_heartbeat_age,
    scheduler_stale_threshold,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_started_at = time.time()


def _cache_backend() -> str:
    """Report which cache backend is active (redis or memory)."""
    from app.utils.cache import get_cache

    try:
        cache = get_cache("health")
    except Exception:
        return "error"
    return "redis" if type(cache).__name__ == "RedisCache" else "memory"


async def _grpc_backend_health() -> dict[str, bool]:
    """HealthCheck RPC against every configured gRPC AI backend."""
    from app.config import AIBackend
    from app.grpc_clients.analysis_client import get_analysis_client

    results: dict[str, bool] = {}
    for backend in AIBackend:
        try:
            client = get_analysis_client(backend)
            results[backend.value] = await client.health_check()
        except Exception:
            results[backend.value] = False
    return results


@router.get("/health")
async def health():
    """Liveness probe with real dependency checks."""
    checks: dict[str, dict] = {}

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:
        checks["database"] = {"ok": False, "error": str(exc)[:200]}

    checks["cache"] = {"ok": True, "backend": _cache_backend()}
    checks["grpc_backends"] = await _grpc_backend_health()

    schedulers: dict[str, dict] = {}
    for name in SCHEDULER_INTERVALS:
        age = scheduler_heartbeat_age(name)
        schedulers[name] = {
            "heartbeat_age_seconds": round(age, 1) if age is not None else None,
            "stale_threshold_seconds": scheduler_stale_threshold(name),
            "stale": age is not None and age > scheduler_stale_threshold(name),
        }
    checks["schedulers"] = schedulers

    database_ok = checks["database"]["ok"]
    any_grpc_down = bool(checks["grpc_backends"]) and not all(checks["grpc_backends"].values())
    any_stale = any(entry["stale"] for entry in schedulers.values())

    if not database_ok:
        status_code, overall = 503, "error"
    elif any_grpc_down or any_stale:
        status_code, overall = 200, "degraded"
    else:
        status_code, overall = 200, "ok"

    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "uptime_seconds": round(time.time() - _started_at, 1),
            "checks": checks,
        },
    )


async def _require_admin(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Dependency that ensures the caller is an admin user."""
    user_id = current_user.get("user_id")
    user = db.query(User).filter(User.id == user_id).first() if user_id else None
    if not user or not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


@router.get("/metrics")
async def metrics(_admin: User = Depends(_require_admin)):
    """Prometheus text metrics (admin only)."""
    gauge("app_uptime_seconds", "Seconds since process start").set(time.time() - _started_at)

    lines = [
        "# HELP scheduler_heartbeat_age_seconds Seconds since the scheduler last heartbeat",
        "# TYPE scheduler_heartbeat_age_seconds gauge",
    ]
    for name in SCHEDULER_INTERVALS:
        age = scheduler_heartbeat_age(name)
        if age is not None:
            lines.append(f'scheduler_heartbeat_age_seconds{{scheduler="{name}"}} {age:.1f}')

    return PlainTextResponse(content=render_prometheus() + "\n".join(lines) + "\n")
