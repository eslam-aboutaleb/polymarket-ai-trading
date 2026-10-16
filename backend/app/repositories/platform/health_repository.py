"""Database connectivity probes for platform health checks."""

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session


class DatabaseHealthRepository:
    def ping(self, db: Session) -> None:
        db.execute(text("SELECT 1"))

    def ping_connection(self, connection: Connection) -> None:
        connection.execute(text("SELECT 1"))
