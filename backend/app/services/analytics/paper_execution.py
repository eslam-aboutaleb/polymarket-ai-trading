"""Frozen execution assumptions and native evidence checks for delayed paper IOC."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.models.trading.paper_execution_intent import PaperExecutionIntent
from app.services.analytics.paper_evidence import EvidenceUnavailable, latest, market_rows, payload
from app.services.market_data.binary_book import reconstruct
from app.services.market_data.book_research import ResearchQuery, book_event, market_spec
from app.services.market_data.replay_capture import digest

VERSION = "latency-paper-IOC-v2"


def assumptions(state):
    # Numeric storage scale is not an economic difference.
    fee = str(Decimal(state.fee_bps).normalize()) if state.fee_bps is not None else None
    return digest([json.loads(state.policy_json), fee])


class ExecutionWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    intent_id: str
    decision_at: AwareDatetime
    eligible_at: AwareDatetime
    deadline_at: AwareDatetime
    evidence_as_of: AwareDatetime
    measured_delay_ms: Decimal = Field(ge=0, le=60000)
    feed_lag_ms: Decimal = Field(ge=0, le=60000)
    condition_id: str = Field(pattern=r"^0x[a-f0-9]{64}$")
    outcome: Literal["YES", "NO"]
    model_probability: Decimal = Field(gt=0, lt=1)
    minimum_edge: Decimal = Field(ge=0, lt=1)
    assumptions_hash: str
    abstention_reason: str | None = None

    def validate_intent(self, db, state, key, token, amount, moment, policy):
        row = (
            db.query(PaperExecutionIntent)
            .filter_by(
                id=self.intent_id,
                user_id=state.user_id,
                account_id=state.account_id,
                intent_key=key,
            )
            .one_or_none()
        )
        if row is None:
            raise EvidenceUnavailable("PAPER_INTENT_MISSING")
        frozen = json.loads(row.request_json)
        if digest(frozen) != row.request_hash:
            raise EvidenceUnavailable("PAPER_INTENT_INTEGRITY_FAILURE")
        supplied = self.model_dump(
            mode="json", exclude={"intent_id", "evidence_as_of", "abstention_reason"}
        )
        if (
            supplied != frozen["window"]
            or frozen["token_id"] != token
            or Decimal(frozen["amount"]) != amount
        ):
            raise EvidenceUnavailable("PAPER_INTENT_EVIDENCE_MISMATCH")
        if self.abstention_reason:
            raise EvidenceUnavailable(self.abstention_reason)
        if assumptions(state) != self.assumptions_hash:
            raise EvidenceUnavailable("PAPER_ASSUMPTIONS_CHANGED")
        elapsed = Decimal(str((self.eligible_at - self.decision_at).total_seconds())) * 1000
        if self.measured_delay_ms < self.feed_lag_ms or elapsed < max(
            self.measured_delay_ms - self.feed_lag_ms, Decimal(policy.latency_ms)
        ):
            raise EvidenceUnavailable("EXECUTION_DELAY_NOT_OBSERVED")
        if self.decision_at > self.eligible_at or self.evidence_as_of < self.eligible_at:
            raise EvidenceUnavailable("EXECUTION_DELAY_NOT_REACHED")
        if self.evidence_as_of > moment:
            raise EvidenceUnavailable("FUTURE_EXECUTION_EVIDENCE")
        if self.evidence_as_of > self.deadline_at:
            raise EvidenceUnavailable("INTENT_EXPIRED")

    def validate_book(self, db, evidence):
        if min(evidence.book.source_at, evidence.book.observed_at) < self.eligible_at:
            raise EvidenceUnavailable("NO_POST_DELAY_BOOK")
        if evidence.condition_id != self.condition_id:
            raise EvidenceUnavailable("INTENDED_CONDITION_MISMATCH")
        row = latest(db, "book", self.evidence_as_of, token_id=evidence.book.token_id)
        peers = (
            db.query(type(row))
            .filter_by(kind="book", token_id=row.token_id, observed_at=row.observed_at)
            .all()
        )
        if len({p.content_hash for p in peers}) > 1:
            raise EvidenceUnavailable("AMBIGUOUS_RECEIPT_ORDER")
        raw = payload(row)["venue"]
        clob, gamma = market_rows(
            db, evidence.condition_id, self.evidence_as_of, evidence.rule_hash
        )
        try:
            spec = market_spec(
                {
                    "raw": {"clob": clob, "gamma": gamma, "market": self.condition_id},
                    "received_at": evidence.book.observed_at.isoformat(),
                    "snapshot_id": row.id,
                },
                ResearchQuery(condition_id=self.condition_id),
                allow_up_down=True,
            )
            intended = spec.yes_token if self.outcome == "YES" else spec.no_token
            if intended != row.token_id or isinstance(raw.get("timestamp"), bool):
                raise ValueError("native identity/clock mismatch")
            source = datetime.fromtimestamp(int(raw["timestamp"]) / 1000, UTC)
            if source != evidence.book.source_at:
                raise ValueError("native clock mismatch")
            for side in ("bids", "asks"):
                for level in raw[side]:
                    if any(isinstance(level[k], bool) for k in ("price", "size")):
                        raise ValueError("boolean level")
            event = book_event(
                {
                    "raw": {**raw, "event_type": "book"},
                    "snapshot_id": row.id,
                    "token_id": row.token_id,
                    "received_at": evidence.book.observed_at,
                    "event_at": source,
                    "connection_id": "paper-rest",
                },
                spec,
            )
            book = reconstruct(
                spec, row.token_id, [event] if event else [], self.evidence_as_of, 60
            )
            if book.grade != "VERIFIED_BOOK":
                raise ValueError("native book invalid")
            for side in ("bids", "asks"):
                expected = [(v.price, v.shares) for v in getattr(book, side)][:10]
                actual = [(v.price, v.shares) for v in getattr(evidence.book, side)]
                if actual != expected:
                    raise ValueError("normalized/native levels disagree")
        except (ValueError, TypeError, KeyError, ArithmeticError, OverflowError, OSError) as exc:
            raise EvidenceUnavailable("INVALID_NATIVE_BOOK") from exc


def classify(result):
    shares = Decimal(result.get("shares", "0"))
    state = "UNVERIFIABLE_DATA"
    if result.get("execution_window"):
        if shares > 0:
            state = "PARTIALLY_FILLED_MODELED" if result["status"] == "partial" else "ESTIMATED"
        elif result.get("reason") in {
            "EDGE_NOT_EXECUTABLE_AFTER_COSTS",
            "INSUFFICIENT_CASH_INVENTORY_OR_DEPTH",
            "BELOW_VENUE_MINIMUM_SIZE",
        }:
            state = "UNFILLED"
    return {
        **result,
        "execution_state": state,
        "exchange_confirmed": False,
        "fill_evidence_grade": "modeled_native_depth"
        if shares and state != "UNVERIFIABLE_DATA"
        else "not_fill_verifiable",
    }
