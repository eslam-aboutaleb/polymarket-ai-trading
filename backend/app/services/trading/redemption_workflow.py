"""Durable redemption workflow. Preview/reconciliation never authorize a broadcast."""

import json
import re
import time
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.models import Account, AppEvent, RedemptionAttempt, TradingControlState, User, UserSettings
from app.models.trading.redemption_intent import (
    RedemptionIntent,
    RedemptionObservation,
    RedemptionWalletLease,
)
from app.security.credential_store import load_wallet_credentials
from app.services.analytics.live_account_reconciliation import canonical, digest
from app.services.trading.live_trading_policy import block_legacy_live_execution
from app.services.trading.redemption_chain import NativeRedemption
from app.utils.time import utc_now

ACTIVE = {"preparing", "unknown", "submitted", "reorged"}


def owner(db, account):
    stored = db.get(Account, account.id)
    user = db.get(User, account.owner_user_id)
    if (
        user is None
        or stored is None
        or stored.owner_user_id != user.id
        or stored.kind != "personal"
    ):
        raise ValueError("REDEMPTION_WALLET_ALLOCATION_UNVERIFIED")
    return user


def read_intent(db, account, intent_id):
    row = db.scalar(
        select(RedemptionIntent)
        .where(
            RedemptionIntent.id == intent_id,
            RedemptionIntent.user_id == account.owner_user_id,
            RedemptionIntent.account_id == account.id,
        )
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise LookupError("Redemption intent unavailable")
    snapshot = json.loads(row.snapshot_json)
    if digest(snapshot) != row.snapshot_hash:
        raise ValueError("REDEMPTION_SNAPSHOT_INTEGRITY_MISMATCH")
    if (
        digest([row.account_id, row.wallet, row.condition_id, row.execution_mode])
        != row.request_hash
    ):
        raise ValueError("REDEMPTION_REQUEST_INTEGRITY_MISMATCH")
    if (
        snapshot["wallet"] != row.wallet
        or snapshot["condition_id"] != row.condition_id
        or snapshot["chain_id"] != row.chain_id
    ):
        raise ValueError("REDEMPTION_SNAPSHOT_IDENTITY_MISMATCH")
    if row.receipt_json is not None and digest(json.loads(row.receipt_json)) != row.receipt_hash:
        raise ValueError("REDEMPTION_RECEIPT_INTEGRITY_MISMATCH")
    return row


def audit(db, row, kind, payload):
    db.add(
        AppEvent(
            user_id=row.user_id,
            account_id=row.account_id,
            source="redemption_gateway",
            aggregate_type="redemption",
            aggregate_id=row.id,
            event_type=kind,
            payload_json=canonical(payload),
        )
    )


def insert(db, model):
    if db.bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as statement
    else:
        from sqlalchemy.dialects.sqlite import insert as statement
    return statement(model)


def create_intent(db, account, condition, key, *, mode="preview", chain=None):
    user = owner(db, account)
    if mode not in {"preview", "submit"} or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", key):
        raise ValueError("REDEMPTION_REQUEST_INVALID")
    if not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition):
        raise ValueError("REDEMPTION_CONDITION_INVALID")
    condition = condition.lower()
    request = digest([account.id, user.wallet_address.lower(), condition, mode])
    identity = digest([account.id, key])
    existing = db.get(RedemptionIntent, identity)
    if existing is not None:
        existing = read_intent(db, account, identity)
        if existing.request_hash != request:
            raise ValueError("REDEMPTION_IDEMPOTENCY_CONFLICT")
        return existing
    native = chain or NativeRedemption()
    try:
        snapshot = native.preview(user.wallet_address, condition)
    finally:
        if chain is None:
            native.close()
    payout = Decimal(snapshot["expected_payout"])
    if not payout.is_finite() or payout < 0 or payout > Decimal(10000):
        raise ValueError("REDEMPTION_VALUE_BOUND_EXCEEDED")
    values = {
        "id": identity,
        "user_id": user.id,
        "account_id": account.id,
        "idempotency_key": key,
        "request_hash": request,
        "execution_mode": mode,
        "chain_id": 137,
        "wallet": user.wallet_address.lower(),
        "condition_id": condition,
        "snapshot_json": canonical(snapshot),
        "snapshot_hash": digest(snapshot),
        "state": "preview" if mode == "preview" else "ready",
        "reason": "NO_REDEEMABLE_CTF_BALANCE" if not any(snapshot["quantities_base"]) else None,
        "created_at": utc_now(),
        "updated_at": utc_now(),
    }
    result = db.execute(
        insert(db, RedemptionIntent).values(**values).on_conflict_do_nothing(index_elements=["id"])
    )
    if result.rowcount:
        row = db.get(RedemptionIntent, identity)
        db.add(
            RedemptionAttempt(
                intent_id=identity,
                user_id=user.id,
                account_id=account.id,
                market_id=condition,
                collection_id=snapshot["collection_ids"][0],
                status="preview",
                amount=float(payout),
            )
        )
        audit(
            db,
            row,
            "redemption_intent_created",
            {"execution_mode": mode, "snapshot_hash": row.snapshot_hash},
        )
    db.commit()
    row = read_intent(db, account, identity)
    if row.request_hash != request:
        raise ValueError("REDEMPTION_IDEMPOTENCY_CONFLICT")
    return row


def _attempt(db, row):
    return db.scalar(select(RedemptionAttempt).where(RedemptionAttempt.intent_id == row.id))


def _release_lease(db, row):
    db.execute(
        update(RedemptionWalletLease)
        .where(
            RedemptionWalletLease.chain_id == row.chain_id,
            RedemptionWalletLease.wallet == row.wallet,
            RedemptionWalletLease.active_intent_id == row.id,
        )
        .values(active_intent_id=None)
    )


def _state(db, row, state, reason=None, **values):
    row.state, row.reason, row.updated_at = state, reason, utc_now()
    for key, value in values.items():
        setattr(row, key, value)
    attempt = _attempt(db, row)
    attempt.status = "preview" if state in {"preview", "ready", "preparing"} else state
    attempt.tx_hash = row.transaction_hash
    attempt.error = reason
    audit(
        db,
        row,
        "redemption_state_changed",
        {"state": state, "reason": reason, "transaction_hash": row.transaction_hash},
    )


def _authorize(db, account, intent_id):
    from app.services.trading.execution.live import _lock_wallet

    user = owner(db, account)
    _lock_wallet(db, user.id)
    settings = db.scalar(
        select(UserSettings)
        .where(UserSettings.user_id == user.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if settings is None or settings.simulation_mode or settings.trading_halted:
        raise ValueError("REDEMPTION_ACCOUNT_RELEASE_BLOCKED")
    count = db.scalar(
        select(func.count())
        .select_from(RedemptionIntent)
        .where(
            RedemptionIntent.user_id == user.id,
            RedemptionIntent.id != intent_id,
            RedemptionIntent.nonce.is_not(None),
            RedemptionIntent.created_at >= utc_now() - timedelta(hours=24),
        )
    )
    if count >= 50:
        raise ValueError("REDEMPTION_DAILY_CAP_EXCEEDED")
    return user


def submit_intent(db, account, intent_id, *, chain=None):
    row = read_intent(db, account, intent_id)
    if row.execution_mode != "submit":
        raise ValueError("REDEMPTION_PREVIEW_CANNOT_SUBMIT")
    # Existing release barrier remains authoritative. A UI flag, request mode,
    # saved intent or favorable payout must never grant signing permission.
    block_legacy_live_execution("redemption")
    user = _authorize(db, account, row.id)
    if row.state != "ready":
        return row  # Restart/duplicate calls reconcile; they never sign again.
    if not db.execute(
        update(RedemptionIntent)
        .where(RedemptionIntent.id == row.id, RedemptionIntent.state == "ready")
        .values(state="preparing")
    ).rowcount:
        db.rollback()
        return read_intent(db, account, intent_id)
    db.execute(
        insert(db, RedemptionWalletLease)
        .values(chain_id=row.chain_id, wallet=row.wallet)
        .on_conflict_do_nothing()
    )
    locked = db.execute(
        update(RedemptionWalletLease)
        .where(
            RedemptionWalletLease.chain_id == row.chain_id,
            RedemptionWalletLease.wallet == row.wallet,
            RedemptionWalletLease.active_intent_id.is_(None),
        )
        .values(active_intent_id=row.id)
    ).rowcount
    if not locked:
        db.rollback()
        raise ValueError("REDEMPTION_WALLET_RECONCILIATION_REQUIRED")
    db.commit()
    row = read_intent(db, account, intent_id)
    native = chain or NativeRedemption()
    try:
        frozen = json.loads(row.snapshot_json)
        fresh = native.preview(row.wallet, row.condition_id)
        for key in (
            "version",
            "wallet",
            "condition_id",
            "target",
            "collateral",
            "underlying",
            "token_ids",
            "quantities_base",
            "numerators",
            "denominator",
            "calldata",
            "expected_payout",
        ):
            if fresh[key] != frozen[key]:
                raise ValueError("REDEMPTION_ENTITLEMENT_CHANGED")
        if not any(fresh["quantities_base"]) or Decimal(fresh["expected_payout"]) <= 0:
            raise ValueError("REDEMPTION_NO_BALANCE")
        credentials = load_wallet_credentials(user.wallet_address)
        if (
            not credentials
            or not credentials.get("private_key")
            or credentials.get("signature_type", 0) != 0
            or str(credentials.get("funder") or row.wallet).lower() != row.wallet
        ):
            raise ValueError("REDEMPTION_SMART_WALLET_REQUIRES_MANUAL_CLAIM")
        tx = native.prepare(row.wallet, fresh)
        row.nonce, row.transaction_json = tx["nonce"], canonical(tx)
        # Reserve the exact nonce durably before signing; uniqueness is global
        # to this chain/wallet even across terminal intents.
        db.commit()
        raw, tx_hash = native.sign(tx, credentials["private_key"])
        _state(
            db, row, "unknown", "REDEMPTION_BROADCAST_OUTCOME_UNVERIFIED", transaction_hash=tx_hash
        )
        db.commit()  # Commit the local signed hash BEFORE the first network send.
        try:
            block_legacy_live_execution("redemption")
            _authorize(db, account, row.id)
            returned_hash = native.broadcast(raw)
            if returned_hash != tx_hash:
                raise ValueError("REDEMPTION_BROADCAST_HASH_MISMATCH")
        except Exception:
            # The node may have accepted the transaction before a timeout.
            # Keep its nonce/hash reserved; reconciliation is the only next step.
            db.rollback()
            return read_intent(db, account, intent_id)
        _state(db, row, "submitted", "REDEMPTION_AWAITING_FINALITY")
        db.commit()
        return row
    except IntegrityError:
        db.rollback()
        row = read_intent(db, account, intent_id)
        _state(db, row, "unknown", "REDEMPTION_NONCE_CONFLICT")
        db.commit()
        return row
    except Exception as exc:
        db.rollback()
        row = read_intent(db, account, intent_id)
        reason = (
            str(exc)
            if isinstance(exc, ValueError) and re.fullmatch(r"REDEMPTION_[A-Z_]{1,100}", str(exc))
            else "REDEMPTION_PREPARATION_UNVERIFIED"
        )
        # No signed hash means this process has never attempted a broadcast.
        # A restart in 'preparing' still needs operator reconciliation.
        _state(
            db,
            row,
            "unknown" if row.nonce is not None else "failed",
            reason,
        )
        if row.nonce is None:
            _release_lease(db, row)
        db.commit()
        return row
    finally:
        if chain is None:
            native.close()


def reconcile_intent(db, account, intent_id, *, chain=None):
    row = read_intent(db, account, intent_id)
    if row.execution_mode == "preview" or row.state == "ready":
        return row
    if row.transaction_hash is None:
        return row  # No nonce reuse or guessed transaction on restart.
    native = chain or NativeRedemption()
    try:
        result = native.reconcile(row, json.loads(row.snapshot_json))
    except Exception as exc:
        reason = (
            str(exc)
            if isinstance(exc, ValueError) and re.fullmatch(r"REDEMPTION_[A-Z_]{1,100}", str(exc))
            else "REDEMPTION_RECEIPT_UNVERIFIED"
        )
        result = {
            "state": "reorged" if row.state == "confirmed" else "unknown",
            "reason": reason,
            "error_type": type(exc).__name__,
        }
    finally:
        if chain is None:
            native.close()
    # Compare-and-swap prevents duplicate workers from creating a second
    # finalized credit event. AppEvent and proof update commit atomically.
    previous = row.state
    from app.services.trading.execution.live import _lock_wallet

    _lock_wallet(db, row.user_id, running=False)
    claimed = db.execute(
        update(RedemptionIntent)
        .where(
            RedemptionIntent.id == row.id,
            RedemptionIntent.state == previous,
            RedemptionIntent.receipt_hash == row.receipt_hash,
        )
        .values(updated_at=utc_now())
    ).rowcount
    if not claimed:
        db.rollback()
        return read_intent(db, account, intent_id)
    proof = canonical(result)
    proof_hash = digest(result)
    db.execute(
        insert(db, RedemptionObservation)
        .values(
            id=digest([row.id, proof_hash]),
            intent_id=row.id,
            proof_hash=proof_hash,
            proof_json=proof,
            observed_at=utc_now(),
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    if result["state"] == previous and row.receipt_hash == proof_hash:
        db.commit()  # Advance the polling watermark without another state/credit event.
        return row
    if result["state"] == "confirmed" and not row.finalized_once:
        row.finalized_once = True
        audit(
            db,
            row,
            "redemption_finalized",
            {
                "transaction_hash": row.transaction_hash,
                "payout": result["payout"],
                "proof_hash": proof_hash,
            },
        )
    _state(
        db,
        row,
        result["state"],
        result.get("reason"),
        receipt_json=proof,
        receipt_hash=proof_hash,
        payout=Decimal(result["payout"]) if result["state"] == "confirmed" else None,
    )
    if previous in {"confirmed", "failed"} and row.state not in {"confirmed", "failed"}:
        control = db.get(TradingControlState, f"user:{row.user_id}")
        if control is None:
            control = TradingControlState(scope=f"user:{row.user_id}", user_id=row.user_id)
            db.add(control)
        control.state = "RECOVERY_REQUIRED"
        control.reason = "REDEMPTION_FINALITY_REVIEW_REQUIRED"
        control.version = (control.version or 0) + 1
        # Do not automatically clear this halt if a later RPC observation recovers.
    if row.state in {"confirmed", "failed"}:
        _release_lease(db, row)
    db.commit()
    return row


def response(db, row):
    snapshot = json.loads(row.snapshot_json)
    attempt = _attempt(db, row)
    return {
        "id": attempt.id,
        "intent_id": row.id,
        "user_id": row.user_id,
        "account_id": row.account_id,
        "market_id": row.condition_id,
        "collection_id": attempt.collection_id,
        "execution_mode": row.execution_mode,
        "status": row.state,
        "preview_only": row.execution_mode == "preview",
        "on_chain_submitted": row.transaction_hash is not None
        and row.state in {"submitted", "confirmed", "reorged"},
        "broadcast_outcome_unknown": row.state == "unknown" and row.transaction_hash is not None,
        "tx_hash": row.transaction_hash,
        "amount": attempt.amount,
        "expected_payout": snapshot["expected_payout"],
        "finalized_payout": str(row.payout) if row.state == "confirmed" else None,
        "nonce": row.nonce,
        "snapshot_hash": row.snapshot_hash,
        "receipt_hash": row.receipt_hash,
        "error": row.reason,
        "created_at": row.created_at,
        "finality_policy": snapshot["finality_policy"],
        "execution_eligible": False,
    }


def reconcile_pending(db, account, *, seconds=15):
    """Bounded restart recovery; this path can only observe receipts, never resend."""
    deadline = time.monotonic() + seconds
    rows = db.scalars(
        select(RedemptionIntent)
        .where(
            RedemptionIntent.account_id == account.id,
            RedemptionIntent.user_id == account.owner_user_id,
            RedemptionIntent.state.in_([*ACTIVE, "confirmed"]),
        )
        .order_by(RedemptionIntent.updated_at, RedemptionIntent.id)
        .limit(3)
    ).all()
    result = []
    for row in rows:
        if time.monotonic() >= deadline:
            break
        native = NativeRedemption()
        native.reader.deadline = min(native.reader.deadline, deadline)
        try:
            result.append(reconcile_intent(db, account, row.id, chain=native).state)
        finally:
            native.close()
    return result
