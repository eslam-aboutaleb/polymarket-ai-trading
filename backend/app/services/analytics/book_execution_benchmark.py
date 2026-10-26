"""Read-only parity against the existing gateway's settled execution observations."""

from decimal import Decimal, localcontext

from app.models import LiveOrder, UserTrade
from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.market_data.binary_book import BinaryBook, MarketSpec, WalkRequest, walk_depth
from app.services.market_data.book_research import ResearchQuery, replay_research
from app.services.market_data.replay_capture import aware
from app.utils.time import utc_now


def compare_execution(db, run, trade_id, account):
    trade = (
        db.query(UserTrade)
        .filter_by(id=trade_id, user_id=account.owner_user_id, account_id=account.id)
        .first()
    )
    order = (
        db.query(LiveOrder)
        .filter_by(trade_id=trade_id, user_id=account.owner_user_id, account_id=account.id)
        .first()
    )
    if trade is None or order is None:
        raise LookupError("execution unavailable")
    unavailable = {
        "status": "abstain",
        "reason": "SETTLED_EXECUTION_UNVERIFIED",
        "measured_price_impact": None,
        "observed_vwap_difference": None,
        "profitability_claim": False,
        "comparable_sample_count": 0,
    }
    if (
        trade.status != "executed"
        or order.state not in {"FILLED", "CANCELLED"}
        or order.response.get("settled") is not True
        or not order.order_hash
        or trade.order_hash != order.order_hash
        or trade.executed_at is None
        or trade.filled_price is None
        or trade.filled_size is None
    ):
        return unavailable
    archived = replay_research(db, run)["result"]
    query = ResearchQuery.model_validate_json(run.input_json)
    if not archived.get("spec"):
        return {**unavailable, "reason": "NO_CAPTURED_BOOK"}
    spec = MarketSpec.model_validate(archived["spec"])
    book = BinaryBook.model_validate(archived["books"][query.outcome])
    if (
        order.token_id != book.token_id
        or trade.token_id != book.token_id
        or trade.market_id != spec.condition_id
        or order.side != query.side
    ):
        return {**unavailable, "reason": "EXECUTION_TOKEN_SIDE_OR_CONDITION_MISMATCH"}
    if (
        aware(order.created_at) < book.as_of
        or book.expires_at is None
        or aware(order.created_at) > book.expires_at
        or aware(trade.executed_at) < aware(order.created_at)
        or aware(trade.executed_at) > book.expires_at
        or aware(trade.executed_at) > utc_now()
    ):
        return {**unavailable, "reason": "EXECUTION_OUTSIDE_CAPTURED_QUOTE_WINDOW"}
    try:
        quantity = Decimal(str(order.response["size_matched"]))
        notional = Decimal(str(order.response["filled_notional"]))
        if (
            not quantity.is_finite()
            or not notional.is_finite()
            or quantity <= 0
            or quantity > min(order.shares, Decimal("1000000000"))
            or notional <= 0
            or notional >= quantity
        ):
            raise ValueError("invalid settled amounts")
    except (KeyError, ValueError, ArithmeticError):
        return unavailable
    estimate = walk_depth(
        spec,
        book,
        WalkRequest(
            side=query.side,
            amount=quantity,
            fee_bps=query.fee_bps,
            fee_version=query.fee_version,
            limit_price=query.limit_price,
        ),
    )
    if estimate.status != "modeled" or estimate.matched_shares != quantity:
        return {**unavailable, "reason": "CAPTURED_SIZE_OR_FEE_POLICY_UNAVAILABLE"}
    with localcontext(ACCOUNTING_CONTEXT):
        actual = notional / quantity
        adverse = actual - estimate.vwap if query.side == "BUY" else estimate.vwap - actual
        return {
            "status": "observed_comparison",
            "trade_id": trade.id,
            "order_hash": order.order_hash,
            "captured_book_hash": book.book_hash,
            "quantity": str(quantity),
            "captured_vwap": str(estimate.vwap),
            "observed_vwap": str(actual),
            "observed_vwap_difference": str(adverse),
            "measured_price_impact": None,
            "fees_compared": False,
            "comparable_sample_count": 1,
            "profitability_claim": False,
            "confidence": "gateway_settled_report; legacy_float_notional_precision",
            "interpretation": (
                "Observed price difference includes latency and market movement; "
                "not causal market impact"
            ),
        }
