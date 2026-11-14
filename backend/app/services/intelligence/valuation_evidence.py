"""Read-side valuation from recorded rules, native books and prior calibration rows.

Caller declarations never establish verified rules, quotes, calibration or ownership.
The numerical primitive remains useful for explicitly hypothetical worked examples.
"""

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.analytics.assessment import VERIFIED_SCORING_VERSION, Assessment
from app.models.trading.user_trade import UserTrade
from app.models.users.account import Account
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.intelligence.binary_valuation import (
    BinaryValuationInput,
    ExactDecimal,
    value_binary_contract,
)
from app.services.intelligence.resolution_rules import (
    get_rule_snapshot_as_of,
    parse_resolution_rule,
)


class RecordedValuationRequest(BaseModel):
    """Identifiers select recorded evidence; callers cannot approve provenance."""

    model_config = ConfigDict(extra="forbid")
    condition_id: str = Field(pattern=r"^0x[a-fA-F0-9]{64}$")
    token_id: str = Field(pattern=r"^[0-9]{1,120}$")
    outcome: Literal["YES", "NO"]
    side: Literal["BUY", "SELL"] = "BUY"
    shares: ExactDecimal = Field(gt=0, le=1_000_000_000)
    as_of: AwareDatetime
    rules_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    forecast_id: int = Field(gt=0)
    account_id: int | None = Field(default=None, gt=0)
    limit_price: ExactDecimal | None = Field(default=None, gt=0, lt=1)
    redemption_cost_per_share: ExactDecimal = Field(default=Decimal(0), ge=0)
    adverse_fill_haircut_per_share: ExactDecimal = Field(default=Decimal(0), ge=0)
    capital_lockup_days: ExactDecimal = Field(default=Decimal(0), ge=0)
    annual_funding_rate: ExactDecimal = Field(default=Decimal(0), ge=0)

    def arithmetic_input(self):
        # These placeholders cannot yield a valuation: the read-side join must
        # replace them with recorded evidence, or return an explicit abstention.
        return BinaryValuationInput(
            **self.model_dump(),
            rule_review_status="unverified",
            calibrated_p_yes=Decimal(".5"),
            p_yes_lower=Decimal(0),
            p_yes_upper=Decimal(1),
            executable_vwap=Decimal(0),
            available_depth_shares=Decimal(0),
            taker_fee_per_share=Decimal(0),
            fee_schedule_version="unverified",
        )


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def calibrated_probability(db, forecast, as_of):
    """Frozen ten-bin empirical calibration; no future labels or duplicate markets.

    A minimum of 20 distinct resolved conditions in the same model/task/bin
    is required. The Wilson interval assumes binomial sampling and describes
    bin frequency uncertainty, not
    a guarantee about this individual event. Insufficient evidence abstains.
    """
    p = Decimal(str(forecast.ai_probability))
    if not p.is_finite() or not 0 <= p <= 1 or not forecast.model_name:
        raise ValueError("ABSTAIN_INVALID_FORECAST")
    cutoff = min(aware(forecast.created_at), as_of)
    bucket = min(9, int(p * 10))
    rows = db.scalars(
        select(Assessment)
        .where(
            Assessment.model_name == forecast.model_name,
            Assessment.trade_history_id.is_(None),
            Assessment.task == forecast.task,
            Assessment.created_at < cutoff,
            Assessment.resolved_at < cutoff,
            Assessment.scoring_version == VERIFIED_SCORING_VERSION,
            Assessment.exclusion_reason.is_(None),
            Assessment.ai_probability.isnot(None),
            Assessment.condition_id != forecast.condition_id,
        )
        .order_by(Assessment.created_at.desc(), Assessment.id.desc())
        .limit(10000)
    ).all()
    seen, records = set(), []
    for row in rows:
        raw = Decimal(str(row.ai_probability))
        if not raw.is_finite() or not 0 <= raw <= 1:
            continue
        condition = str(row.condition_id or "").lower()
        label = str(row.resolved_outcome or "").upper()
        if (
            not condition
            or condition in seen
            or label not in {"YES", "NO"}
            or min(9, int(raw * 10)) != bucket
        ):
            continue
        seen.add(condition)
        records.append([row.id, condition, str(raw), label, aware(row.resolved_at).isoformat()])
    if len(records) < 20:
        raise ValueError("ABSTAIN_INSUFFICIENT_CALIBRATION_HISTORY")
    n = Decimal(len(records))
    wins = Decimal(sum(r[3] == "YES" for r in records))
    frequency = wins / n
    z2 = Decimal("1.96") ** 2
    denominator = 1 + z2 / n
    centre = (frequency + z2 / (2 * n)) / denominator
    radius = (
        Decimal("1.96") * (frequency * (1 - frequency) / n + z2 / (4 * n * n)).sqrt() / denominator
    )
    digest = hashlib.sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest()
    return (
        frequency,
        max(Decimal(0), centre - radius),
        min(Decimal(1), centre + radius),
        {
            "method": "ten-bin-frequency-wilson95-v1",
            "sample_count": len(records),
            "training_available_before": cutoff.isoformat(),
            "training_sha256": digest,
            "forecast_id": forecast.id,
            "raw_probability": str(p),
            "interpretation": "empirical bin frequency; research only; no approved Kelly parameter",
        },
    )


def inventory_at(db, user_id, account_id, token, as_of):
    rows = db.scalars(
        select(UserTrade).where(
            UserTrade.user_id == user_id,
            UserTrade.account_id == account_id,
            UserTrade.token_id == token,
            UserTrade.created_at <= as_of,
        )
    ).all()
    total = Decimal(0)
    for row in rows:
        if row.status not in {"executed", "failed", "cancelled"}:
            raise ValueError("ABSTAIN_UNRECONCILED_INVENTORY")
        if row.status != "executed":
            continue
        if (
            row.filled_size is None
            or row.executed_at is None
            or aware(row.executed_at) > as_of
            or (row.updated_at is not None and aware(row.updated_at) > as_of)
        ):
            raise ValueError("ABSTAIN_INVENTORY_NOT_KNOWN_AS_OF")
        shares = Decimal(str(row.filled_size))
        if not shares.is_finite() or shares < 0 or row.action.lower() not in {"buy", "sell"}:
            raise ValueError("ABSTAIN_INVALID_FILL_EVIDENCE")
        total += shares if row.action.lower() == "buy" else -shares
    return total


def value_recorded_contract(db: Session, inp: BinaryValuationInput, user_id: int):
    with localcontext(ACCOUNTING_CONTEXT):
        return _value_recorded_contract(db, inp, user_id)


def _value_recorded_contract(db: Session, inp: BinaryValuationInput, user_id: int):
    reasons, provenance = [], {}
    updates = {"kelly_fraction_cap": Decimal(0)}
    try:
        if inp.as_of.tzinfo is None or inp.as_of > datetime.now(UTC):
            raise ValueError("ABSTAIN_INVALID_AS_OF")
        rule = get_rule_snapshot_as_of(db, inp.condition_id, inp.as_of)
        if rule is None or rule.review_status != "verified" or rule.source_hash != inp.rules_hash:
            raise ValueError("ABSTAIN_UNVERIFIED_RECORDED_RULE")
        if rule.token_outcomes.get(inp.outcome) != inp.token_id:
            raise ValueError("ABSTAIN_TOKEN_RULE_MISMATCH")
        if inp.settlement_payout != 1 or inp.settlement_currency != "USDC":
            raise ValueError("ABSTAIN_UNSUPPORTED_PAYOUT")
        if inp.forecast_id is None:
            raise ValueError("ABSTAIN_MISSING_RECORDED_FORECAST")
        forecast = db.get(Assessment, inp.forecast_id)
        if (
            forecast is None
            or forecast.condition_id != inp.condition_id
            or aware(forecast.created_at) > inp.as_of
        ):
            raise ValueError("ABSTAIN_FORECAST_IDENTITY_OR_TIME")
        if (
            forecast.trade_history_id is not None
            and db.scalar(
                select(UserTrade.id)
                .where(
                    UserTrade.source_trade_history_id == forecast.trade_history_id,
                    UserTrade.user_id == user_id,
                )
                .limit(1)
            )
            is None
        ):
            raise ValueError("ABSTAIN_FORECAST_OWNERSHIP")
        probability, low, high, calibration = calibrated_probability(db, forecast, inp.as_of)
        from app.services.market_data.book_research import ResearchQuery, load_analysis

        query = ResearchQuery(
            condition_id=inp.condition_id,
            as_of=inp.as_of,
            outcome=inp.outcome,
            side=inp.side,
            sizes=[inp.shares],
            fee_bps=Decimal(0),
            fee_version="venue-zero-fee-v1",
            limit_price=inp.limit_price,
        )
        analysis, values = load_analysis(db, query)
        if analysis.get("status") != "modeled" or not analysis.get("estimates"):
            raise ValueError("ABSTAIN_MISSING_NATIVE_BOOK")
        meta = next(v for v in values if v["snapshot_id"] == analysis["metadata_id"])
        raw = meta["raw"]["clob"]
        parsed = parse_resolution_rule(
            inp.condition_id, raw, datetime.fromisoformat(meta["received_at"])
        )
        if parsed.source_hash != rule.source_hash:
            raise ValueError("ABSTAIN_BOOK_RULE_VERSION_MISMATCH")
        estimate = analysis["estimates"][0]
        if (
            estimate["status"] != "modeled"
            or estimate.get("partial")
            or Decimal(estimate["matched_shares"]) != inp.shares
        ):
            raise ValueError("ABSTAIN_INSUFFICIENT_OR_ROUNDED_DEPTH")
        from app.services.market_data.valuation_fees import archived_taker_fee

        fee, fee_version = archived_taker_fee(meta["raw"], estimate["curve"])
        spec = analysis["spec"]
        if inp.shares % Decimal(spec["lot"]) != 0:
            raise ValueError("ABSTAIN_INVALID_SHARE_LOT")
        owned = None
        if inp.side == "SELL":
            account = db.get(Account, inp.account_id) if inp.account_id else None
            if account is None or account.owner_user_id != user_id:
                raise ValueError("ABSTAIN_ACCOUNT_OWNERSHIP")
            owned = inventory_at(db, user_id, account.id, inp.token_id, inp.as_of)
        top = analysis["top_of_book"]["YES"]
        market_p = ((Decimal(top["best_bid"]) + Decimal(top["best_ask"])) / 2) if top else None
        updates.update(
            calibrated_p_yes=probability,
            p_yes_lower=low,
            p_yes_upper=high,
            executable_vwap=Decimal(estimate["vwap"]),
            available_depth_shares=Decimal(estimate["matched_shares"]),
            taker_fee_per_share=fee / inp.shares,
            fee_schedule_version=fee_version,
            market_implied_p=market_p,
            owned_shares=owned,
            rule_review_status="verified",
            share_lot=Decimal(spec["lot"]),
            tick_size=Decimal(spec["tick"]),
        )
        provenance = {
            "calibration": calibration,
            "rule_hash": rule.source_hash,
            "book_hash": estimate["book_hash"],
            "metadata_id": analysis["metadata_id"],
            "book_received_at": analysis["books"][inp.outcome]["as_of"],
            "price_tick": spec["tick"],
            "lot": spec["lot"],
            "fee_rounding": "conservative upper bound; no fee paid or rebate claimed",
            "confidence": "counterfactual displayed depth; no actual fill claimed",
        }
    except (ValueError, KeyError, TypeError, ArithmeticError, StopIteration) as exc:
        reason = str(exc)
        reasons.append(
            reason if reason.startswith("ABSTAIN_") else "ABSTAIN_INVALID_RECORDED_EVIDENCE"
        )
    if reasons:
        updates["rule_review_status"] = "unverified"
    result = value_binary_contract(inp.model_copy(update=updates))
    if reasons:
        result.abstain_reasons = list(dict.fromkeys(reasons + result.abstain_reasons))
    result.provenance = provenance
    return result
