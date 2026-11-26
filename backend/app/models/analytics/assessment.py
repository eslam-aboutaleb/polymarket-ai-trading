"""Assessment model for AI trade evaluation"""

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text, event

from app.models.base import Base
from app.utils.time import utc_now

#: Vocabulary of a *verified* binary resolution. The resolver writes the
#: canonical "YES"/"NO"; rows written by the legacy resolver carry the raw
#: Gamma label ("Yes", "No", "Alice"), so the check is case-insensitive and
#: only these labels may become a calibration label. The scoring service
#: and every aggregate query share this one vocabulary, so a legacy
#: non-binary label is excluded everywhere instead of only in the summary.
BINARY_YES_LABELS = frozenset({"yes", "y", "true", "1"})
BINARY_NO_LABELS = frozenset({"no", "n", "false", "0"})
BINARY_OUTCOME_LABELS = BINARY_YES_LABELS | BINARY_NO_LABELS
VERIFIED_SCORING_VERSION = "brier-binary-v2"

#: Reason recorded on a row whose ``ai_probability`` could not be read.
#: A row with this reason is not a pre-calibration (legacy) row: it is a
#: verdict that was written without a usable probability.
NULL_PROBABILITY_REASON = "unusable_probability"


class Assessment(Base):
    """AI assessment of a trade opportunity"""

    __tablename__ = "assessments"

    id = Column(Integer, primary_key=True, index=True)
    # Nullable: opportunity-scan verdicts have no source trade row
    # (forecast calibration, plan 01) — only copy-trade evals do.
    trade_history_id = Column(Integer, ForeignKey("trade_history.id"), nullable=True, index=True)
    ai_score = Column(Float, nullable=False)  # 0-100 confidence score
    reasoning = Column(Text, nullable=False)
    recommendation = Column(String(50), nullable=False)  # copy, avoid, hold, etc.
    risk_level = Column(String(20), nullable=False)  # low, medium, high
    market_sentiment = Column(String(50), nullable=True)  # bullish, bearish, neutral
    confidence = Column(Float, nullable=False)  # 0-100
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    # ── Forecast calibration (plan 01) ──────────────────────────
    #: AI's calibrated probability the forecast resolves true (0.0-1.0).
    ai_probability = Column(Float, nullable=True)
    #: Market YES price when the verdict was produced.
    market_price_at_assessment = Column(Float, nullable=True)
    #: Winning outcome once the market resolved ("YES"/"NO"/…).
    resolved_outcome = Column(String(20), nullable=True)
    #: Brier score written by the nightly resolution job.
    brier_score = Column(Float, nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    #: Scoring rule that produced ``brier_score`` (e.g. "brier-binary-v2").
    #: A change of scoring rule is then distinguishable from the rows
    #: written under the previous one.
    scoring_version = Column(String(32), nullable=True)
    #: Why the row was excluded from scoring (one of the
    #: ``forecast_resolution.ExclusionReason`` values). Null while the
    #: row is unresolved or once it has been scored.
    exclusion_reason = Column(String(40), nullable=True)
    #: Why ``ai_probability`` is NULL on this row. Null on every row that
    #: carries a probability, and on legacy rows written before the
    #: calibration columns existed (those are counted as legacy, not as
    #: a known-reason null).
    probability_null_reason = Column(String(40), nullable=True)
    #: Model attribution so accuracy can be aggregated per model.
    model_name = Column(String(100), nullable=True)
    provider = Column(String(50), nullable=True)
    task = Column(String(50), nullable=True)
    #: Market identity for resolution matching (independent of the
    #: trade_history link, which is absent for scan verdicts).
    condition_id = Column(String(100), nullable=True)

    # ── Model governance (plan 04) ──────────────────────────────
    #: Short content hash of the prompt store that produced this
    #: verdict. Every prompt change is a new version, so calibration
    #: impact is measurable without hand-numbered prompt releases.
    prompt_version = Column(String(16), nullable=True)
    #: A/B experiment this verdict belonged to, when any.
    experiment_id = Column(String(64), nullable=True)
    #: ``control`` or ``variant`` — the arm of that experiment.
    variant = Column(String(16), nullable=True)
    #: Model tier actually used (CHEAP/STANDARD/STRONG/WRITER). Null
    #: when tiering was disabled, which is why it must never be
    #: treated as a foreign key.
    llm_tier = Column(String(16), nullable=True)

    # ── Immutable forecast identity and provenance (issue #60) ──
    #: Random identity of this forecast run. Two genuine revisions are
    #: always distinct runs even when their rounded fields coincide.
    forecast_uuid = Column(String(36), nullable=True, unique=True, index=True)
    #: Replay-safe request identity: a caller idempotency key, or the SHA-256
    #: of the full-precision decision inputs. Unique, so a duplicate request
    #: can never become a second sample (nullable for legacy rows).
    request_key = Column(String(64), nullable=True, unique=True, index=True)
    #: CLOB token the stated probability is about, and what the number means.
    token_id = Column(String(100), nullable=True)
    #: ``p_yes`` = P(the YES outcome of the market resolves true). It is never
    #: P(this order is profitable).
    p_interpretation = Column(String(24), nullable=True)
    #: JSON ``{token_id: outcome_label}`` mapping in force at decision time.
    outcome_mapping_json = Column(Text, nullable=True)
    #: Resolution-rule hash in force when the forecast was produced.
    rule_hash = Column(String(64), nullable=True)
    #: Evidence snapshot the forecast was grounded on, when any.
    evidence_snapshot_id = Column(String(64), nullable=True)
    calibrator_version = Column(String(32), nullable=True)
    #: The moment the forecaster produced the number (UTC). Only evidence
    #: observed at or before this instant may justify it.
    prediction_instant = Column(DateTime(timezone=True), nullable=True)
    #: Scheduled market end at decision time and the horizon to it, seconds.
    market_close_at = Column(DateTime(timezone=True), nullable=True)
    horizon_seconds = Column(Integer, nullable=True)
    #: Event the market belongs to: the independence unit for evaluation.
    event_group = Column(String(100), nullable=True)
    #: Contemporaneous benchmark quote (the market-implied probability).
    benchmark_price = Column(Float, nullable=True)
    benchmark_bid = Column(Float, nullable=True)
    benchmark_ask = Column(Float, nullable=True)
    benchmark_observed_at = Column(DateTime(timezone=True), nullable=True)
    benchmark_source = Column(String(40), nullable=True)
    #: Seconds between the quote and the prediction (never negative).
    quote_age_seconds = Column(Float, nullable=True)

    def __repr__(self):
        return f"<Assessment(trade_id={self.trade_history_id}, score={self.ai_score})>"


#: Columns fixed when the forecast is recorded. They are written once; a later
#: UPDATE that changes any of them is a bug or tampering, so it raises.
IMMUTABLE_FORECAST_COLUMNS = (
    "condition_id",
    "ai_probability",
    "market_price_at_assessment",
    "model_name",
    "task",
    "prompt_version",
    "created_at",
    "forecast_uuid",
    "request_key",
    "token_id",
    "p_interpretation",
    "outcome_mapping_json",
    "rule_hash",
    "evidence_snapshot_id",
    "calibrator_version",
    "prediction_instant",
    "market_close_at",
    "horizon_seconds",
    "event_group",
    "benchmark_price",
    "benchmark_bid",
    "benchmark_ask",
    "benchmark_observed_at",
    "benchmark_source",
    "quote_age_seconds",
)


class ImmutableForecastError(RuntimeError):
    """Raised when a recorded forecast field is edited after the fact."""


@event.listens_for(Assessment, "before_update")
def _reject_forecast_mutation(_mapper, connection, target):
    """Reject edits of recorded fields, comparing against the stored row.

    The stored value is read through the flush connection, so the guard holds
    for expired instances too. A legacy NULL provenance column may be filled
    once; every other change raises. This is an application-level guard: it
    does not stop raw SQL, which an operator must keep to deliberate
    migrations.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import select

    state = sa_inspect(target)
    changed = [n for n in IMMUTABLE_FORECAST_COLUMNS if state.attrs[n].history.has_changes()]
    if not changed:
        return
    table = Assessment.__table__
    stored = connection.execute(
        select(*[table.c[n] for n in changed]).where(table.c.id == target.id)
    ).one_or_none()
    if stored is None:
        return
    always_set = {"ai_probability", "condition_id", "created_at"}
    for name, old in zip(changed, stored, strict=True):
        if old is None and name not in always_set:
            continue
        new = getattr(target, name)
        if old != new:
            raise ImmutableForecastError(f"forecast field {name!r} is immutable once recorded")
