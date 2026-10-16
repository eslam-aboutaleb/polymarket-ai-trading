"""SQLAlchemy repository foundation.

Repositories are the persistence boundary. They receive a request or worker
session and expose intent-named methods rather than query builders. Domain
repositories own SQLAlchemy query construction; services own business policy
and the unit-of-work flow. Repositories stay deliberately thin and leave
transaction ownership explicit to the caller unless a method documents
otherwise.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

ModelT = TypeVar("ModelT")


class SQLAlchemyRepository(Generic[ModelT]):
    """Generic data-access surface for one ORM model."""

    model: type[ModelT]

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, entity_id: Any) -> ModelT | None:
        return self.db.get(self.model, entity_id)

    def get_or_raise(self, entity_id: Any) -> ModelT:
        entity = self.get(entity_id)
        if entity is None:
            raise ValueError(f"{self.model.__name__} {entity_id} not found")
        return entity

    def first_by(self, **filters: Any) -> ModelT | None:
        stmt = select(self.model).filter_by(**filters).limit(1)
        return self.db.execute(stmt).scalar_one_or_none()

    def list_all(self, *, limit: int | None = None, offset: int = 0) -> list[ModelT]:
        stmt = select(self.model).offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.db.execute(stmt).scalars().all())

    def list_by(self, **filters: Any) -> list[ModelT]:
        stmt = select(self.model).filter_by(**filters)
        return list(self.db.execute(stmt).scalars().all())

    def exists(self, entity_id: Any) -> bool:
        return self.get(entity_id) is not None

    def add(self, entity: ModelT, *, flush: bool = True) -> ModelT:
        self.db.add(entity)
        if flush:
            self.db.flush()
        return entity

    def add_all(self, entities: list[ModelT], *, flush: bool = True) -> list[ModelT]:
        self.db.add_all(entities)
        if flush:
            self.db.flush()
        return entities

    def delete(self, entity: ModelT, *, flush: bool = True) -> None:
        self.db.delete(entity)
        if flush:
            self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def refresh(self, entity: ModelT) -> ModelT:
        self.db.refresh(entity)
        return entity
