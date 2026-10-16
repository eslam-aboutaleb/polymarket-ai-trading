"""ASGI entry point and compatibility exports for application lifecycle hooks."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.application import add_security_headers, create_app
from app.config import get_settings
from app.lifecycle.background_services import start_background_services
from app.lifecycle.runtime import application_lifespan
from app.lifecycle.validation import (
    validate_database_connectivity,
    validate_startup_security,
)
from app.logging_config import configure_logging
from app.security.crypto import get_fernet_keyring
from app.utils.database import (
    SessionLocal,
    engine,
)

__all__ = [
    "SessionLocal",
    "add_security_headers",
    "app",
    "engine",
    "general_exception_handler",
    "lifespan",
]

configure_logging()

logger = logging.getLogger(__name__)
settings = get_settings()

# The debug route and worker process use these module-level lifecycle handles.
_background_tasks: list[asyncio.Task] = []


def _validate_startup_security() -> None:
    """Keep startup security checks available through the ASGI entry point."""
    validate_startup_security(settings, get_fernet_keyring)


def _validate_database_connectivity() -> None:
    """Validate database connectivity before serving traffic."""

    def _check() -> None:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    validate_database_connectivity(_check)


def _sync_admin_wallets() -> None:
    """Synchronize the configured administrator wallets during startup."""
    raw = settings.admin_wallets.strip()
    if not raw and not settings.admin_sync_allow_empty:
        return

    db = SessionLocal()
    try:
        from app.services.users.admin_sync_service import sync_admin_wallets as sync

        sync(
            db,
            settings.admin_wallets,
            allow_empty=settings.admin_sync_allow_empty,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def _start_background_services() -> None:
    """Start schedulers and attach long-running tasks to this process."""
    await start_background_services(_background_tasks, session_factory=SessionLocal)


@asynccontextmanager
async def lifespan(app):
    """ASGI lifecycle using the current entry-point dependencies."""
    async with application_lifespan(
        app,
        settings=settings,
        background_tasks=_background_tasks,
        validate_startup_security=_validate_startup_security,
        validate_database_connectivity=_validate_database_connectivity,
        sync_admin_wallets=_sync_admin_wallets,
        start_background_services=_start_background_services,
    ):
        yield


async def general_exception_handler(request: Request, exc: Exception):
    """Log unhandled errors and mask details outside local environments."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    detail = str(exc) if settings.is_local_environment else "Internal server error"
    return JSONResponse(status_code=500, content={"detail": detail})


app = create_app(
    settings=settings,
    lifespan=lifespan,
    exception_handler=general_exception_handler,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.reload)
