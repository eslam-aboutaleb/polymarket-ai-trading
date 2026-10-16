"""FastAPI application factory and HTTP middleware wiring."""

from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routes import register_routers
from app.config import Settings
from app.middleware.request_logger import RequestLogMiddleware
from app.security.rate_limit import limiter


async def add_security_headers(request: Request, call_next):
    """Add browser security headers to API responses."""
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


def create_app(
    *,
    settings: Settings,
    lifespan,
    exception_handler: Callable,
) -> FastAPI:
    """Build and wire the HTTP application from runtime configuration."""
    application = FastAPI(
        title=settings.api_title,
        description=settings.api_description,
        version=settings.api_version,
        lifespan=lifespan,
    )

    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    application.add_middleware(SlowAPIMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    application.middleware("http")(add_security_headers)
    application.add_middleware(RequestLogMiddleware)
    register_routers(application, debug_enabled=settings.debug_endpoints_active)

    @application.get("/")
    async def root():
        """Describe the API root and documentation links."""
        return {
            "message": "Welcome to Polymarket AI Trading API",
            "docs": "/docs",
            "redoc": "/redoc",
        }

    application.add_exception_handler(Exception, exception_handler)
    return application
