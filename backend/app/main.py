"""ASGI entry point and compatibility exports for application lifecycle hooks."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse

from app.application import add_security_headers, create_app
from app.config import get_settings
from app.lifecycle.background_services import start_background_services
from app.lifecycle.runtime import application_lifespan
from app.lifecycle.validation import (
    sync_admin_wallets,
    validate_database_connectivity,
    validate_startup_security,
)
from app.logging_config import configure_logging
from app.security.crypto import get_fernet_keyring
from app.utils.database import check_database_connectivity, session_scope

__all__ = ["add_security_headers", "app", "general_exception_handler", "lifespan"]

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
    validate_database_connectivity(check_database_connectivity)


def _sync_admin_wallets() -> None:
    """Synchronize the configured administrator wallets during startup."""
    sync_admin_wallets(settings, session_scope)


async def _start_background_services() -> None:
    """Start schedulers and attach long-running tasks to this process."""
    await start_background_services(_background_tasks)


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
