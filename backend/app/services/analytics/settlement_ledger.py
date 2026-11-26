"""Server-owned, append-only settlement transition ledger (#24).

The previous state is always read from this ledger, never from the caller.
Transitions are hash-chained per account/condition/token, validated against a
fixed state machine and idempotent on identical content. Nothing here signs,
submits, redeems or credits cash.
"""

import hashlib
import json
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.analytics.settlement_transition import (
    CONDITION_LEVEL_TOKEN,
    SettlementTransition,
)
from app.services.analytics.settlement_lifecycle import SettlementState as S

SCHEMA_VERSION = "settlement-ledger-v1"
HEX32 = re.compile(r"0x[0-9a-f]{64}")
EVIDENCE_CLASSES = frozenset(
    {"chain_verified", "application_recorded", "venue_reported", "operator_asserted"}
)
HISTORY_BOUND = 500

# Allowed moves. Terminal evidence states may only be superseded by an explicit
# reversal/dispute record; they are never silently rewritten.
ALLOWED = {
    None: {S.TRADING_OPEN, S.TRADING_CLOSED, S.RESOLUTION_PROPOSED, S.FINAL, S.VOID},
    S.TRADING_OPEN: {S.TRADING_CLOSED, S.RESOLUTION_PROPOSED},
    S.TRADING_CLOSED: {S.RESOLUTION_PROPOSED, S.FINAL, S.VOID},
    S.RESOLUTION_PROPOSED: {S.DISPUTED, S.CHALLENGED, S.FINAL, S.VOID},
    S.DISPUTED: {S.RESOLUTION_PROPOSED, S.CHALLENGED, S.FINAL, S.VOID},
    S.CHALLENGED: {S.RESOLUTION_PROPOSED, S.DISPUTED, S.FINAL, S.VOID},
    S.FINAL: {S.REDEEMABLE, S.TX_PENDING, S.TX_CONFIRMED, S.REVERSED, S.DISPUTED, S.VOID},
    S.VOID: {S.REDEEMABLE, S.TX_PENDING, S.TX_CONFIRMED, S.REVERSED},
    S.REDEEMABLE: {S.TX_PENDING, S.TX_CONFIRMED, S.REVERSED, S.DISPUTED},
    S.TX_PENDING: {S.TX_CONFIRMED, S.TX_FAILED, S.REVERSED},
    S.TX_FAILED: {S.TX_PENDING, S.TX_CONFIRMED, S.REVERSED},
    S.TX_CONFIRMED: {S.REVERSED},
    S.REVERSED: {S.RESOLUTION_PROPOSED, S.FINAL, S.VOID},
}
# Only server-verified chain evidence may declare a transaction state.
CHAIN_ONLY = frozenset({S.TX_PENDING, S.TX_CONFIRMED, S.TX_FAILED, S.REDEEMABLE})


class TransitionRejected(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _record_payload(row_fields: dict) -> dict:
    return {
        key: (aware(value).isoformat() if isinstance(value, datetime) else value)
        for key, value in row_fields.items()
    }


def row_hash(row: SettlementTransition) -> str:
    return digest(
        _record_payload(
            {
                "user_id": row.user_id,
                "account_id": row.account_id,
                "condition_id": row.condition_id,
                "token_id": row.token_id,
                "sequence": row.sequence,
                "state": row.state,
                "previous_state": row.previous_state,
                "evidence_class": row.evidence_class,
                "rule_hash": row.rule_hash,
                "evidence": json.loads(row.evidence_json),
                "observed_at": row.observed_at,
                "prev_hash": row.prev_hash,
                "schema_version": row.schema_version,
            }
        )
    )


def _scoped(account, condition_id, token_id):
    return (
        SettlementTransition.user_id == account.owner_user_id,
        SettlementTransition.account_id == account.id,
        SettlementTransition.condition_id == condition_id,
        SettlementTransition.token_id == token_id,
    )


def latest(db, account, condition_id, token_id=CONDITION_LEVEL_TOKEN):
    return db.scalar(
        select(SettlementTransition)
        .where(*_scoped(account, condition_id, token_id))
        .order_by(SettlementTransition.sequence.desc())
        .limit(1)
    )


def append_transition(
    db,
    account,
    *,
    condition_id: str,
    state: S,
    observed_at: datetime,
    evidence_class: str,
    evidence: dict,
    token_id: str | None = None,
    rule_hash: str | None = None,
    server_derived: bool = False,
    now: datetime | None = None,
):
    """Append one validated transition and return ``(row, created)``.

    ``server_derived`` is set only by code that verified the evidence itself
    (for example a canonical Polygon receipt). API callers can never claim it.
    """
    now = now or datetime.now(UTC)
    condition_id = str(condition_id).strip().lower()
    token = token_id or CONDITION_LEVEL_TOKEN
    state = S(state)
    if not HEX32.fullmatch(condition_id):
        raise TransitionRejected("INVALID_CONDITION_ID")
    if evidence_class not in EVIDENCE_CLASSES:
        raise TransitionRejected("UNKNOWN_EVIDENCE_CLASS")
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise TransitionRejected("NAIVE_TIMESTAMP")
    observed_at = aware(observed_at)
    if observed_at > now:
        raise TransitionRejected("FUTURE_OBSERVATION")
    if (evidence_class == "chain_verified" or state in CHAIN_ONLY) and not server_derived:
        raise TransitionRejected("CHAIN_EVIDENCE_MUST_BE_SERVER_DERIVED")
    if state == S.TRADING_OPEN and evidence_class == "chain_verified":
        raise TransitionRejected("UNSUPPORTED_EVIDENCE_FOR_STATE")
    if rule_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", rule_hash):
        raise TransitionRejected("INVALID_RULE_HASH")
    if len(canonical(evidence)) > 16384:
        raise TransitionRejected("EVIDENCE_TOO_LARGE")
    for _ in range(2):
        previous = latest(db, account, condition_id, token)
        prior_state = S(previous.state) if previous else None
        if previous is not None and observed_at < aware(previous.observed_at):
            raise TransitionRejected("OBSERVATION_BEFORE_PREVIOUS_TRANSITION")
        candidate = SettlementTransition(
            user_id=account.owner_user_id,
            account_id=account.id,
            condition_id=condition_id,
            token_id=token,
            sequence=(previous.sequence + 1) if previous else 1,
            state=state.value,
            previous_state=prior_state.value if prior_state else None,
            evidence_class=evidence_class,
            rule_hash=rule_hash,
            evidence_json=canonical(evidence),
            observed_at=observed_at,
            recorded_at=now,
            prev_hash=previous.id if previous else None,
            schema_version=SCHEMA_VERSION,
        )
        if (
            previous is not None
            and previous.state == state.value
            and previous.evidence_class == evidence_class
            and previous.rule_hash == rule_hash
            and previous.evidence_json == candidate.evidence_json
        ):
            return previous, False  # idempotent replay of the same observation
        if previous is not None and previous.state == state.value:
            raise TransitionRejected("STATE_UNCHANGED_WITH_DIFFERENT_EVIDENCE")
        if state not in ALLOWED.get(prior_state, set()):
            origin = prior_state.value if prior_state else "NONE"
            raise TransitionRejected(f"ILLEGAL_TRANSITION_{origin}_TO_{state.value}")
        candidate.id = row_hash(candidate)
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            return candidate, True
        except IntegrityError:
            # A concurrent writer took this sequence. Re-read once and re-validate.
            continue
    raise TransitionRejected("CONCURRENT_TRANSITION_CONFLICT")


def history(db, account, condition_id: str, token_id: str | None = None):
    condition_id = condition_id.strip().lower()
    query = select(SettlementTransition).where(
        SettlementTransition.user_id == account.owner_user_id,
        SettlementTransition.account_id == account.id,
        SettlementTransition.condition_id == condition_id,
    )
    if token_id is not None:
        query = query.where(SettlementTransition.token_id == token_id)
    rows = db.scalars(
        query.order_by(SettlementTransition.token_id, SettlementTransition.sequence).limit(
            HISTORY_BOUND + 1
        )
    ).all()
    if len(rows) > HISTORY_BOUND:
        raise TransitionRejected("HISTORY_BOUND_EXCEEDED")
    return rows


def verify_chain(rows) -> list[str]:
    """Return integrity problems for rows of a single account/condition."""
    problems, by_token = [], {}
    for row in rows:
        by_token.setdefault(row.token_id, []).append(row)
    for token, items in by_token.items():
        previous = None
        for index, row in enumerate(sorted(items, key=lambda item: item.sequence), start=1):
            if row.sequence != index:
                problems.append(f"SEQUENCE_GAP:{token}:{row.sequence}")
            if row.id != row_hash(row):
                problems.append(f"HASH_MISMATCH:{token}:{row.sequence}")
            if (row.prev_hash or None) != (previous.id if previous else None):
                problems.append(f"CHAIN_BROKEN:{token}:{row.sequence}")
            if (row.previous_state or None) != (previous.state if previous else None):
                problems.append(f"PREVIOUS_STATE_MISMATCH:{token}:{row.sequence}")
            previous = row
    return problems


def serialize(row) -> dict:
    return {
        "id": row.id,
        "condition_id": row.condition_id,
        "token_id": row.token_id,
        "sequence": row.sequence,
        "state": row.state,
        "previous_state": row.previous_state,
        "evidence_class": row.evidence_class,
        "rule_hash": row.rule_hash,
        "evidence": json.loads(row.evidence_json),
        "observed_at": aware(row.observed_at).isoformat(),
        "recorded_at": aware(row.recorded_at).isoformat(),
        "prev_hash": row.prev_hash,
        "schema_version": row.schema_version,
    }
