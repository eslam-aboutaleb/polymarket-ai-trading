"""Application event-log persistence queries."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.platform.app_event import AppEvent


class AppEventRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def list_events(
        self,
        *,
        user_id: int | None = None,
        aggregate_type: str | None = None,
        aggregate_id: str | None = None,
        event_types: list[str] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
    ) -> list[AppEvent]:
        stmt = select(AppEvent)
        if user_id is not None:
            stmt = stmt.where(AppEvent.user_id == user_id)
        if aggregate_type:
            stmt = stmt.where(AppEvent.aggregate_type == aggregate_type)
        if aggregate_id is not None:
            stmt = stmt.where(AppEvent.aggregate_id == str(aggregate_id))
        if event_types:
            stmt = stmt.where(AppEvent.event_type.in_(event_types))
        if since is not None:
            stmt = stmt.where(AppEvent.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(AppEvent.occurred_at <= until)
        stmt = stmt.order_by(AppEvent.occurred_at.desc(), AppEvent.id.desc()).limit(
            max(1, min(int(limit), 1000))
        )
        return list(self.db.execute(stmt).scalars().all())
