"""Admin state-change audit persistence."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.platform.audit_log import AdminAuditLog


class AdminAuditLogRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def add(self, row: AdminAuditLog) -> AdminAuditLog:
        self.db.add(row)
        return row
