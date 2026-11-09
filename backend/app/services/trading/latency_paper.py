"""Paper latency orchestration: commit decision, capture after delay, journal once."""

import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from app.config import get_settings
from app.models import Account, AccountSettings, UserSettings
from app.models.trading.latency_arb_config import LatencyArbConfig
from app.models.trading.paper_account import PaperAccount, PaperOrderRecord
from app.models.trading.paper_execution_intent import PaperExecutionIntent
from app.services.analytics.paper_accounting import (
    PaperPolicy,
    account_state,
    decimal,
    find_paper_intent,
    submit_paper_order,
    transaction,
)
from app.services.analytics.paper_equity import owner
from app.services.analytics.paper_evidence import refresh_evidence
from app.services.analytics.paper_execution import ExecutionWindow, assumptions, classify
from app.services.market_data.replay_capture import aware, digest
from app.services.trading.pre_trade_gate import apply_global_safety_caps
from app.utils.time import utc_now


class IntentConflict(ValueError):
    pass


def freeze(db, settings, opportunity, token, amount, delay, account_id, minimum_edge):
    account = owner(db, settings.user_id, account_id)
    key = f"latency:{opportunity.market.condition_id}:{opportunity.window_start_epoch}"
    with transaction(db):
        state = account_state(db, settings.user_id, account.id)
        row = (
            db.query(PaperExecutionIntent).filter_by(account_id=account.id, intent_key=key).first()
        )
        if row is None:
            policy = PaperPolicy.model_validate_json(state.policy_json)
            decision = aware(opportunity.detected_at)
            lag, measured = Decimal(str(opportunity.feed_lag_ms)), Decimal(str(delay))
            if not lag.is_finite() or not measured.is_finite() or not 0 <= lag <= measured <= 60000:
                raise ValueError("unknown execution timing")
            if decision > utc_now():
                raise ValueError("future decision")
            eligible = decision + timedelta(
                milliseconds=float(max(measured - lag, Decimal(policy.latency_ms)))
            )
            probability = Decimal(str(opportunity.p_model))
            if opportunity.side not in {"up", "down"}:
                raise ValueError("unknown intended outcome")
            probability = probability if opportunity.side == "up" else 1 - probability
            window = ExecutionWindow(
                intent_id=str(uuid4()),
                decision_at=decision,
                eligible_at=eligible,
                deadline_at=decision
                + timedelta(milliseconds=get_settings().latency_arb_max_latency_ms - float(lag)),
                evidence_as_of=decision,
                measured_delay_ms=measured,
                feed_lag_ms=lag,
                condition_id=opportunity.market.condition_id,
                outcome="YES" if opportunity.side == "up" else "NO",
                model_probability=probability,
                minimum_edge=Decimal(str(minimum_edge)),
                assumptions_hash=assumptions(state),
            )
            body = {
                "policy": policy.model_dump(mode="json"),
                "fee_bps": str(Decimal(state.fee_bps).normalize())
                if state.fee_bps is not None
                else None,
                "token_id": token,
                "amount": str(decimal(amount)),
                "window": window.model_dump(
                    mode="json", exclude={"intent_id", "evidence_as_of", "abstention_reason"}
                ),
            }
            row = PaperExecutionIntent(
                id=window.intent_id,
                account_id=account.id,
                user_id=settings.user_id,
                intent_key=key,
                request_json=json.dumps(body, sort_keys=True),
                request_hash=digest(body),
                created_at=utc_now(),
            )
            db.add(row)
            db.flush()
        body = json.loads(row.request_json)
        if (
            digest(body) != row.request_hash
            or body["token_id"] != token
            or Decimal(body["amount"]) != Decimal(str(amount))
        ):
            raise IntentConflict("paper intent identity conflict")
        identity = row.id, account.id, key, body
    db.commit()  # No locks across await; cancellation leaves a recoverable immutable decision.
    return identity


def risk_after_capture(db, settings, account_id, amount):
    settings = (
        db.query(UserSettings)
        .filter_by(user_id=settings.user_id)
        .with_for_update()
        .populate_existing()
        .one()
    )
    account = owner(db, settings.user_id, account_id)
    active = settings
    if account.kind == "sub":
        active = (
            db.query(AccountSettings)
            .filter_by(account_id=account.id)
            .with_for_update()
            .populate_existing()
            .one_or_none()
        )
    if active is None or not active.simulation_mode or not settings.simulation_mode:
        return "PAPER_MODE_DISABLED_OR_HALTED"
    for scope, scope_account in ((settings, None), (active, account.id)):
        # AccountSettings deliberately lacks user_id; supply its verified owner
        # while preserving the exact account risk settings and scoped aggregates.
        risk_settings = (
            scope
            if isinstance(scope, UserSettings)
            else SimpleNamespace(
                **{column.name: getattr(scope, column.name) for column in scope.__table__.columns},
                user_id=settings.user_id,
            )
        )
        capped, rejection = apply_global_safety_caps(
            risk_settings, float(amount), db, account_id=scope_account
        )
        if rejection or Decimal(str(capped)) < Decimal(str(amount)):
            return "RISK_LIMITS_CHANGED"
    config = (
        db.query(LatencyArbConfig)
        .filter_by(user_id=settings.user_id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if config and (
        not config.enabled or owner(db, settings.user_id, config.account_id).id != account.id
    ):
        return "STRATEGY_DISABLED_OR_ACCOUNT_CHANGED"
    from app.services.trading.latency_paper_risk import check

    state = account_state(db, settings.user_id, account.id)
    states = db.query(PaperAccount).filter_by(user_id=settings.user_id).all()
    reason = check(db, states, settings, Decimal(str(amount)), utc_now())
    if reason:
        return reason
    reason = check(
        db,
        [state],
        active,
        Decimal(str(amount)),
        utc_now(),
        config.daily_loss_limit if config else None,
    )
    if reason:
        return reason
    return None


def response(result):
    return {
        **classify(result),
        "fill_status": result["status"],
        "status": "simulated" if result["status"] in {"filled", "partial"} else "unfilled",
        "size": float(result["notional_usdc"]),
    }


async def execute(
    db, settings, opportunity, token_id, amount, delay, *, account_id=None, minimum_edge=0
):
    key = f"latency:{opportunity.market.condition_id}:{opportunity.window_start_epoch}"
    base = {
        "user_id": settings.user_id,
        "token_id": token_id,
        "side": "BUY",
        "amount": Decimal(str(amount)),
        "strategy": "latency_arb",
        "intent_key": key,
        "account_id": account_id,
    }
    if not get_settings().paper_journal_v2_enabled:
        result = submit_paper_order(db, **base)
        db.commit()
        return response(result)
    try:
        intent_id, account_id, key, body = freeze(
            db, settings, opportunity, token_id, amount, delay, account_id, minimum_edge
        )
    except IntentConflict:
        raise
    except (ValueError, TypeError, ArithmeticError, AttributeError):
        # Unknown timing is an explicit journal outcome, never an invented delay.
        result = submit_paper_order(db, **base)
        db.commit()
        return response(result)
    base["account_id"] = account_id
    previous = find_paper_intent(db, settings.user_id, key, account_id)
    if previous is not None:
        return response(previous)
    eligible = datetime.fromisoformat(body["window"]["eligible_at"])
    deadline = datetime.fromisoformat(body["window"]["deadline_at"])
    if eligible <= deadline and utc_now() <= deadline:
        remaining = (eligible - utc_now()).total_seconds()
        if remaining > 0:
            await asyncio.sleep(remaining)
        await refresh_evidence(db, opportunity.market.condition_id, allow_up_down=True)
    with transaction(db):
        # Stable owner -> account ordering serializes owner-wide risk across
        # subaccounts and prevents checking before waiting on another writer.
        db.query(UserSettings).filter_by(user_id=settings.user_id).with_for_update().one()
        db.query(Account).filter_by(owner_user_id=settings.user_id).order_by(
            Account.id
        ).with_for_update().all()
        account_state(db, settings.user_id, account_id)
        window = ExecutionWindow(
            **body["window"],
            intent_id=intent_id,
            evidence_as_of=utc_now(),
            abstention_reason=risk_after_capture(db, settings, account_id, amount),
        )
        result = submit_paper_order(db, **base, execution_window=window.model_dump(mode="json"))
    db.commit()
    return response(result)


def history(db, user_id, account_id, limit=20, offset=0):
    account = owner(db, user_id, account_id)
    query = db.query(PaperExecutionIntent).filter_by(account_id=account.id, user_id=user_id)
    rows = (
        query.order_by(PaperExecutionIntent.created_at.desc(), PaperExecutionIntent.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    results = {
        r.intent_key: json.loads(r.result_json)
        for r in db.query(PaperOrderRecord)
        .filter(
            PaperOrderRecord.account_id == account.id,
            PaperOrderRecord.user_id == user_id,
            PaperOrderRecord.intent_key.in_([r.intent_key for r in rows]),
        )
        .all()
    }
    return {
        "total": query.count(),
        "limit": limit,
        "offset": offset,
        "intents": [
            {
                "intent_id": row.id,
                "created_at": aware(row.created_at).isoformat(),
                "intent": json.loads(row.request_json),
                "result": classify(
                    results.get(
                        row.intent_key,
                        {
                            "status": "unfilled",
                            "shares": "0",
                            "notional_usdc": "0",
                            "fee_usdc": "0",
                            "realized_pnl": "0",
                            "reason": "PENDING_CAPTURE_OR_RECOVERY",
                        },
                    )
                ),
            }
            for row in rows
        ],
        "quality": "modeled_not_exchange_confirmed",
    }
