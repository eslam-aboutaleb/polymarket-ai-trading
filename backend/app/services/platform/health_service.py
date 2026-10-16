"""Platform health-check operations."""

from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.repositories.platform.health_repository import DatabaseHealthRepository


def ping_database(db: Session) -> None:
    DatabaseHealthRepository().ping(db)


def ping_connection(connection: Connection) -> None:
    DatabaseHealthRepository().ping_connection(connection)
