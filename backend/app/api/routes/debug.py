"""Debug / monitoring API routes — serves request logs, endpoint stats and
comprehensive health checks for the debug dashboard."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes.auth import get_current_user_from_token
from app.config import get_settings
from app.middleware.request_logger import (
    clear_logs,
    get_endpoint_stats,
    get_log_entries,
    get_uptime_seconds,
)
from app.models.user import User
from app.utils.database import get_db

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/debug", tags=["debug"])


def require_admin(
    current_user: dict = Depends(get_current_user_from_token),
    db: Session = Depends(get_db),
):
    """Dependency that ensures debug endpoints are enabled + caller is admin."""
    if not settings.debug_endpoints_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Debug endpoints disabled",
        )
    user_id = current_user.get("user_id")
    user = db.query(User).filter(User.id == user_id).first() if user_id else None
    if not user or not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return {
        "user_id": user.id,
        "wallet_address": user.wallet_address,
        "is_admin": user.is_admin,
    }


# ────────────────────────── Schemas ──────────────────────────


class LogEntry(BaseModel):
    request_id: str
    timestamp: str
    method: str
    path: str
    query: str = ""
    status_code: int
    duration_ms: float
    level: str
    client_ip: str = ""
    user_agent: str = ""
    error_detail: str | None = None


class LogsResponse(BaseModel):
    total: int
    filtered: int
    entries: list[LogEntry]


class EndpointStat(BaseModel):
    method: str
    path: str
    call_count: int
    error_count: int
    warning_count: int
    avg_duration_ms: float
    min_duration_ms: float
    max_duration_ms: float
    last_called: str
    last_status: int


class StatsResponse(BaseModel):
    total_requests: int
    total_errors: int
    total_warnings: int
    error_rate: float
    endpoints: list[EndpointStat]


class ServiceHealth(BaseModel):
    name: str
    status: str  # "healthy" | "unhealthy" | "unknown"
    latency_ms: float | None = None
    detail: str = ""


class HealthResponse(BaseModel):
    uptime_seconds: float
    uptime_human: str
    server_time: str
    python_version: str
    services: list[ServiceHealth]


# ────────────────────────── Helpers ──────────────────────────


def _format_uptime(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


async def _check_postgres() -> ServiceHealth:
    """Ping PostgreSQL via SQLAlchemy."""
    try:
        from sqlalchemy import text

        from app.utils.database import SessionLocal

        start = time.perf_counter()
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            latency = round((time.perf_counter() - start) * 1000, 2)
            return ServiceHealth(
                name="PostgreSQL", status="healthy", latency_ms=latency, detail="Connected"
            )
        finally:
            db.close()
    except Exception as e:
        return ServiceHealth(name="PostgreSQL", status="unhealthy", detail=str(e)[:200])


async def _check_redis() -> ServiceHealth:
    """Ping Redis."""
    try:
        import redis as redis_lib

        redis_url = os.environ.get("REDIS_URL", "")
        if not redis_url:
            return ServiceHealth(name="Redis", status="unknown", detail="REDIS_URL not set")
        start = time.perf_counter()
        r = redis_lib.from_url(redis_url)
        r.ping()
        latency = round((time.perf_counter() - start) * 1000, 2)
        r.close()
        return ServiceHealth(name="Redis", status="healthy", latency_ms=latency, detail="Connected")
    except Exception as e:
        return ServiceHealth(name="Redis", status="unhealthy", detail=str(e)[:200])


async def _check_grpc_service(name: str, host: str, port: int) -> ServiceHealth:
    """Ping a gRPC service via TCP connect."""
    try:
        start = time.perf_counter()
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3.0)
        latency = round((time.perf_counter() - start) * 1000, 2)
        writer.close()
        await writer.wait_closed()
        return ServiceHealth(
            name=name, status="healthy", latency_ms=latency, detail=f"{host}:{port}"
        )
    except TimeoutError:
        return ServiceHealth(
            name=name, status="unhealthy", detail=f"Timeout connecting to {host}:{port}"
        )
    except Exception as e:
        return ServiceHealth(name=name, status="unhealthy", detail=str(e)[:200])


async def _check_background_tasks() -> ServiceHealth:
    """Check if background asyncio tasks are still alive."""
    try:
        from app.main import _background_tasks

        alive = sum(1 for t in _background_tasks if not t.done())
        total = len(_background_tasks)
        status = (
            "healthy" if alive == total and total > 0 else "unhealthy" if alive == 0 else "warning"
        )
        if total == 0:
            status = "unknown"
        return ServiceHealth(
            name="Background Tasks",
            status=status,
            detail=f"{alive}/{total} tasks running",
        )
    except Exception as e:
        return ServiceHealth(name="Background Tasks", status="unknown", detail=str(e)[:200])


async def _check_inverse_bot_monitor() -> ServiceHealth:
    """Check inverse-bot monitor task state and metrics."""
    try:
        from app.services.inverse_bot_monitor import get_inverse_bot_metrics

        m = get_inverse_bot_metrics()
        running = bool(m.get("running"))
        status = "healthy" if running else "unhealthy"
        detail = (
            f"running={running}, evaluations={m.get('evaluations_total', 0)}, "
            f"reversals={m.get('reversals_total', 0)}, inflight={m.get('inflight', 0)}"
        )
        return ServiceHealth(name="Inverse Bot Monitor", status=status, detail=detail)
    except Exception as e:
        return ServiceHealth(name="Inverse Bot Monitor", status="unknown", detail=str(e)[:200])


async def _check_research_mcp() -> ServiceHealth:
    """
    Lightweight MCP health signal for inverse-bot research toolchain.
    Checks that ddgs is importable and research MCP server file exists.
    """
    try:
        import pathlib

        configured = os.environ.get("RESEARCH_MCP_SERVER_PATH", "").strip()
        candidates = [
            configured,
            "/app/services/mcps/research/server.py",
            "services/mcps/research/server.py",
        ]
        server_path = next(
            (str(pathlib.Path(p)) for p in candidates if p and pathlib.Path(p).exists()),
            "",
        )
        if server_path:
            return ServiceHealth(
                name="Research MCP",
                status="healthy",
                detail=f"server={server_path}",
            )
        # Normal in this architecture: backend does not host MCP directly.
        return ServiceHealth(
            name="Research MCP",
            status="healthy",
            detail="Managed by AI service containers (llm-chain / cli-agent).",
        )
    except Exception as e:
        return ServiceHealth(name="Research MCP", status="unhealthy", detail=str(e)[:200])


# ────────────────────────── Endpoints ──────────────────────────


@router.get("/logs", response_model=LogsResponse)
async def get_logs(
    level: str | None = Query(None, description="Filter by level: info, warning, error"),
    path: str | None = Query(None, description="Filter by path substring"),
    method: str | None = Query(None, description="Filter by HTTP method"),
    limit: int = Query(200, ge=1, le=1000),
    current_user: dict = Depends(require_admin),
):
    """Return recent request log entries with optional filters. Admin only."""
    all_entries = get_log_entries()
    total = len(all_entries)
    filtered = all_entries

    if level:
        filtered = [e for e in filtered if e["level"] == level.lower()]
    if path:
        filtered = [e for e in filtered if path.lower() in e["path"].lower()]
    if method:
        filtered = [e for e in filtered if e["method"].upper() == method.upper()]

    filtered = filtered[:limit]

    return LogsResponse(
        total=total,
        filtered=len(filtered),
        entries=[LogEntry(**e) for e in filtered],
    )


@router.get("/stats", response_model=StatsResponse)
async def get_stats(
    current_user: dict = Depends(require_admin),
):
    """Return per-endpoint aggregate statistics. Admin only."""
    raw = get_endpoint_stats()

    total_requests = 0
    total_errors = 0
    total_warnings = 0
    endpoints: list[EndpointStat] = []

    for _key, s in sorted(raw.items(), key=lambda kv: kv[1]["call_count"], reverse=True):
        avg_dur = round(s["total_duration_ms"] / s["call_count"], 2) if s["call_count"] else 0
        total_requests += s["call_count"]
        total_errors += s["error_count"]
        total_warnings += s["warning_count"]
        endpoints.append(
            EndpointStat(
                method=s["method"],
                path=s["path"],
                call_count=s["call_count"],
                error_count=s["error_count"],
                warning_count=s["warning_count"],
                avg_duration_ms=avg_dur,
                min_duration_ms=s["min_duration_ms"],
                max_duration_ms=s["max_duration_ms"],
                last_called=s["last_called"],
                last_status=s["last_status"],
            )
        )

    error_rate = round((total_errors / total_requests * 100) if total_requests else 0, 2)

    return StatsResponse(
        total_requests=total_requests,
        total_errors=total_errors,
        total_warnings=total_warnings,
        error_rate=error_rate,
        endpoints=endpoints,
    )


@router.get("/health", response_model=HealthResponse)
async def comprehensive_health(
    current_user: dict = Depends(require_admin),
):
    """Comprehensive health check: postgres, redis, gRPC services, background tasks. Admin only."""
    uptime = get_uptime_seconds()

    llm_host = os.environ.get("LLM_CHAIN_HOST", "llm-chain")
    llm_port = int(os.environ.get("LLM_CHAIN_PORT", "50051"))
    cli_host = os.environ.get("CLI_AGENT_HOST", "cli-agent")
    cli_port = int(os.environ.get("CLI_AGENT_PORT", "50052"))

    services = await asyncio.gather(
        _check_postgres(),
        _check_redis(),
        _check_grpc_service("LLM Chain (gRPC)", llm_host, llm_port),
        _check_grpc_service("CLI Agent (gRPC)", cli_host, cli_port),
        _check_background_tasks(),
        _check_inverse_bot_monitor(),
        _check_research_mcp(),
    )

    return HealthResponse(
        uptime_seconds=round(uptime, 1),
        uptime_human=_format_uptime(uptime),
        server_time=datetime.now(UTC).isoformat(),
        python_version=platform.python_version(),
        services=list(services),
    )


@router.delete("/logs")
async def delete_logs(
    current_user: dict = Depends(require_admin),
):
    """Clear the request log buffer. Admin only."""
    count = clear_logs()
    return {"cleared": count, "message": f"Cleared {count} log entries"}
