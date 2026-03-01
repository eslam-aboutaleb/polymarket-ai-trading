"""
Request logging middleware — captures every API call into an in-memory ring
buffer so the debug dashboard can display request logs, errors and stats.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
import logging

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ────────────────────────── Ring buffer (singleton) ──────────────────────────

MAX_LOG_ENTRIES = 1000

_log_buffer: deque[Dict[str, Any]] = deque(maxlen=MAX_LOG_ENTRIES)
_endpoint_stats: Dict[str, Dict[str, Any]] = {}
_app_start_time: float = time.time()

# Lock for thread-safe writes (asyncio is single-threaded per loop,
# but the deque is also safe for append/pop from different coroutines)
_stats_lock = asyncio.Lock()


def get_log_entries() -> List[Dict[str, Any]]:
    """Return a shallow copy of all log entries (newest first)."""
    return list(reversed(_log_buffer))


def get_endpoint_stats() -> Dict[str, Dict[str, Any]]:
    """Return per-endpoint aggregate stats."""
    return dict(_endpoint_stats)


def get_uptime_seconds() -> float:
    return time.time() - _app_start_time


def clear_logs() -> int:
    """Clear the ring buffer. Returns the number of entries cleared."""
    count = len(_log_buffer)
    _log_buffer.clear()
    return count


def _classify_level(status_code: int) -> str:
    if status_code >= 500:
        return "error"
    if status_code >= 400:
        return "warning"
    return "info"


# Skip logging for these prefixes (avoid noise & recursion)
_SKIP_PREFIXES = ("/health", "/docs", "/openapi.json", "/redoc", "/favicon")
_SENSITIVE_QUERY_KEYS = {
    "token",
    "access_token",
    "refresh_token",
    "key",
    "api_key",
    "signature",
    "password",
    "secret",
    "authorization",
}


def _hash_client_ip(ip: str) -> str:
    if not ip or ip == "unknown":
        return "unknown"
    salt = settings.debug_log_hash_salt or "debug-salt-unset"
    digest = hmac.new(
        salt.encode("utf-8"),
        ip.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest[:16]


def _sanitize_query(query: str) -> str:
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    redacted: list[tuple[str, str]] = []
    for key, value in pairs:
        if key.lower() in _SENSITIVE_QUERY_KEYS:
            redacted.append((key, "***"))
        else:
            redacted.append((key, value))
    return urlencode(redacted, doseq=True)


# ────────────────────────── Middleware ──────────────────────────


class RequestLogMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that logs every request/response cycle.
    Captures: request_id, method, path, status_code, duration_ms,
    timestamp, client_ip, user_agent, and error details for 4xx/5xx.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path

        # Skip noise endpoints
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return await call_next(request)

        request_id = str(uuid.uuid4())[:8]
        method = request.method
        raw_client_ip = request.client.host if request.client else "unknown"
        client_ip = _hash_client_ip(raw_client_ip)
        user_agent = request.headers.get("user-agent", "")[:120]
        query = _sanitize_query(str(request.url.query) if request.url.query else "")
        start = time.perf_counter()

        error_detail: Optional[str] = None
        status_code = 500  # default in case of unhandled exception

        try:
            response = await call_next(request)
            status_code = response.status_code
            if status_code >= 500:
                error_detail = "internal_server_error"
            elif status_code >= 400:
                error_detail = "client_error"

        except Exception:
            status_code = 500
            error_detail = "internal_server_error"
            # Re-raise so FastAPI's exception handlers still fire
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            level = _classify_level(status_code)

            entry: Dict[str, Any] = {
                "request_id": request_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "method": method,
                "path": path,
                "query": query,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "level": level,
                "client_ip": client_ip,
                "user_agent": user_agent,
            }
            if error_detail:
                entry["error_detail"] = error_detail

            _log_buffer.append(entry)

            # Update aggregate stats
            stat_key = f"{method} {path}"
            stats = _endpoint_stats.get(stat_key)
            if stats is None:
                stats = {
                    "method": method,
                    "path": path,
                    "call_count": 0,
                    "error_count": 0,
                    "warning_count": 0,
                    "total_duration_ms": 0.0,
                    "min_duration_ms": duration_ms,
                    "max_duration_ms": duration_ms,
                    "last_called": entry["timestamp"],
                    "last_status": status_code,
                }
                _endpoint_stats[stat_key] = stats

            stats["call_count"] += 1
            stats["total_duration_ms"] += duration_ms
            stats["last_called"] = entry["timestamp"]
            stats["last_status"] = status_code
            if duration_ms < stats["min_duration_ms"]:
                stats["min_duration_ms"] = duration_ms
            if duration_ms > stats["max_duration_ms"]:
                stats["max_duration_ms"] = duration_ms
            if level == "error":
                stats["error_count"] += 1
            elif level == "warning":
                stats["warning_count"] += 1

        return response
