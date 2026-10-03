"""Database connection and session management"""

from sqlalchemy import create_engine
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
