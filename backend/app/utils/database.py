"""Database connection and session management"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.config import get_settings

settings = get_settings()


def _database_connect_args() -> dict:
    if settings.is_local_environment:
        return {}
    if settings.database_url.startswith("postgresql"):
        return {"sslmode": "require"}
    return {}


def _database_engine_kwargs() -> dict:
    kwargs = {
        "echo": settings.is_local_environment,
        "future": True,
        "poolclass": QueuePool,
        "pool_size": 10,
        "max_overflow": 20,
        "pool_timeout": 30,
        "pool_recycle": 1800,
        "pool_pre_ping": True,
    }
    connect_args = _database_connect_args()
    if connect_args:
        kwargs["connect_args"] = connect_args
    return kwargs


# Create database engine
engine = create_engine(settings.database_url, **_database_engine_kwargs())

# Create session factory
SessionLocal = sessionmaker(
    bind=engine,
    class_=Session,
    expire_on_commit=False,
)


def get_db() -> Session:
    """Dependency to get DB session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope around a series of operations.

    The single session-opening primitive for background/worker code: the
    session is committed on success, rolled back on any exception, and
    always closed. Replaces the ad-hoc ``SessionLocal()`` +
    ``try/finally: db.close()`` blocks in the scheduler services.
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_database_connectivity() -> None:
    """Verify the configured engine can open a basic database connection."""
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
