"""Append-only, hash-chained settlement lifecycle transitions (#24).

One row is one observed state change for an account/condition/token. Rows are
never updated or deleted: a correction is a later transition that links to the
previous row hash. Database triggers enforce this below the ORM.
"""

from sqlalchemy import (
    DDL,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)

from app.models.base import Base
from app.utils.time import utc_now

CONDITION_LEVEL_TOKEN = "*"  # noqa: S105 - sentinel, not a credential


class SettlementTransition(Base):
    __tablename__ = "settlement_transitions"

    id = Column(String(64), primary_key=True)  # sha256 of the canonical record
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    condition_id = Column(String(66), nullable=False)
    token_id = Column(String(200), nullable=False, default=CONDITION_LEVEL_TOKEN)
    sequence = Column(Integer, nullable=False)
    state = Column(String(24), nullable=False)
    previous_state = Column(String(24), nullable=True)
    evidence_class = Column(String(24), nullable=False)
    rule_hash = Column(String(64), nullable=True)
    evidence_json = Column(Text, nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    prev_hash = Column(String(64), nullable=True)
    schema_version = Column(String(32), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "account_id", "condition_id", "token_id", "sequence", name="uq_settlement_sequence"
        ),
        UniqueConstraint(
            "account_id", "condition_id", "token_id", "prev_hash", name="uq_settlement_prev_hash"
        ),
        Index("ix_settlement_transitions_condition", "account_id", "condition_id"),
    )


def immutability_ddl(dialect: str) -> list[str]:
    if dialect == "postgresql":
        return [
            "CREATE OR REPLACE FUNCTION settlement_transitions_immutable() RETURNS trigger AS $$ "
            "BEGIN RAISE EXCEPTION 'settlement_transitions is append-only'; END; "
            "$$ LANGUAGE plpgsql",
            "CREATE TRIGGER settlement_transitions_no_change BEFORE UPDATE OR DELETE "
            "ON settlement_transitions FOR EACH ROW EXECUTE FUNCTION "
            "settlement_transitions_immutable()",
        ]
    if dialect == "sqlite":
        return [
            f"CREATE TRIGGER settlement_transitions_no_{op} BEFORE {op.upper()} "
            "ON settlement_transitions BEGIN SELECT RAISE(ABORT, "
            "'settlement_transitions is append-only'); END"
            for op in ("update", "delete")
        ]
    return []


for _dialect in ("postgresql", "sqlite"):
    for _statement in immutability_ddl(_dialect):
        event.listen(
            SettlementTransition.__table__,
            "after_create",
            DDL(_statement).execute_if(dialect=_dialect),
        )
