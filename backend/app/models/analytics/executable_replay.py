"""Private frozen strategy family and write-once research output."""

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.models.base import Base
from app.utils.time import utc_now


class ExecutableReplayRun(Base):
    __tablename__ = "executable_replay_runs"
    id = Column(String(36), primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False, index=True)
    backtest_run_id = Column(Integer, ForeignKey("backtest_runs.id"), nullable=True, unique=True)
    dataset_id = Column(String(64), ForeignKey("observation_datasets.id"), nullable=True)
    input_hash = Column(String(64), nullable=False)
    input_json = Column(Text, nullable=False)
    evidence_json = Column(Text, nullable=False)
    evidence_hash = Column(String(64), nullable=False)
    result_json = Column(Text, nullable=True)
    result_hash = Column(String(64), nullable=True)
    frozen_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    executed_at = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint(
            "account_id", "input_hash", "evidence_hash", name="uq_executable_frozen_input"
        ),
    )


class ExecutableSignalSource(Base):
    __tablename__ = "executable_signal_sources"
    run_id = Column(String(36), ForeignKey("executable_replay_runs.id"), primary_key=True)
    capture_id = Column(String(64), ForeignKey("replay_captures.id"), primary_key=True)
