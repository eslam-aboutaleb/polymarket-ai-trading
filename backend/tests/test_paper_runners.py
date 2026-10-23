import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from paper_account_fixtures import db as _paper_db
from paper_account_fixtures import evidence, order

from app.models import PaperAccount, UserSettings, UserTrade
from app.services.analytics import paper_accounting as paper
from app.services.trading import (
    copy_trade_service,
    inverse_bot_monitor,
    latency_arb_service,
    stop_loss_monitor,
)
from app.services.trading.execution.base import OrderPlacementRequest, PaperExecutionEngine

db = _paper_db


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize("strategy", ["copy", "latency", "engine"])
def test_identical_native_events_book_identical_economics_without_signing(db, strategy):
    session, _ = db
    settings = session.get(UserSettings, 1)
    settings.copy_trading_enabled = True
    settings.require_ai_approval = False
    settings.risk_mode = "fixed_amount"
    settings.fixed_trade_amount = 10
    session.commit()
    forbidden = AssertionError("paper accessed private execution")
    with (
        patch("py_clob_client.client.ClobClient.post_order", side_effect=forbidden),
        patch("py_clob_client.client.ClobClient.create_order", side_effect=forbidden),
        patch.object(copy_trade_service, "load_wallet_credentials", side_effect=forbidden),
    ):
        if strategy == "engine":
            result = run(
                PaperExecutionEngine(db=session).place_order(
                    OrderPlacementRequest(
                        user_id=1,
                        account_id=1,
                        token_id="11",
                        side="BUY",
                        size=25,
                        price=0.4,
                        order_type="IOC",
                        metadata={"intent_key": "engine"},
                    )
                )
            )
            assert result.executed_size == 25
        elif strategy == "latency":
            result = run(
                latency_arb_service._execute_paper(
                    session,
                    settings,
                    SimpleNamespace(
                        market=SimpleNamespace(condition_id="native"), window_start_epoch=1
                    ),
                    "11",
                    0.99,
                    10,
                    1,
                )
            )
            assert result["status"] == "simulated"
        else:
            result = run(
                copy_trade_service.execute_copy_trade(
                    session,
                    1,
                    "0x" + "aa" * 20,
                    "native",
                    "11",
                    "BUY",
                    0.4,
                    50,
                    idempotency_key="copy",
                )
            )
            assert result["executed"], result
    value = paper.value_account(session, session.get(PaperAccount, 1))
    assert Decimal(value["cash"]) == 990 and Decimal(value["positions"]["11"]["shares"]) == 25
    assert session.query(UserTrade).count() == 1


@pytest.mark.parametrize("kind", ["stop_loss", "take_profit"])
def test_protective_exit_uses_real_inventory_and_leaves_partial_remainder(db, kind):
    session, clock = db
    order(session)
    clock[0] += timedelta(seconds=1)
    evidence(session, clock, bid="0.70", ask="0.71", depth="10")
    exit_order = SimpleNamespace(
        id=1,
        user_id=1,
        account_id=1,
        token_id="11",
        size=25,
        status="active",
        market_id="native",
        order_hash=None,
        triggered_at=None,
    )
    function = getattr(stop_loss_monitor, f"_execute_{kind}_simulated")
    run(function(session, exit_order, 0.99))
    assert exit_order.status == "active" and exit_order.size == 15
    assert Decimal(paper.restore(session, session.get(PaperAccount, 1)).realized_pnl) == 3
    run(function(session, exit_order, 0.99))
    assert exit_order.size == 15 and session.query(UserTrade).count() == 2


def test_inverse_two_legs_cannot_double_book_or_invent_sell_proceeds(db):
    session, clock = db
    order(session)
    clock[0] += timedelta(seconds=1)
    evidence(session, clock, bid="0.70", ask="0.71")
    evidence(session, clock, token="22", bid="0.24", ask="0.25")
    settings = session.get(UserSettings, 1)
    row = SimpleNamespace(
        id=12, user_id=1, account_id=1, token_id="11", size_mode_override="full_notional"
    )
    args = {
        "db": session,
        "row": row,
        "settings": settings,
        "from_size": 25,
        "from_price": 0.99,
        "to_token_id": "22",
        "to_outcome": "No",
        "to_price": 0.01,
    }
    result = inverse_bot_monitor._simulate_reversal(**args)
    assert result["status"] == "simulated", result
    session.commit()
    assert inverse_bot_monitor._simulate_reversal(**args) == result
    assert session.query(UserTrade).count() == 3
    inverse_bot_monitor._record_reversal_trades(
        session,
        user_id=1,
        condition_id="native",
        from_token_id="11",
        from_size=25,
        execution=result,
        to_token_id="22",
        buy_notional=result["buy_notional"],
    )
    assert session.query(UserTrade).count() == 3
    holdings = paper.restore(session, session.get(PaperAccount, 1))
    assert not holdings.positions["11"].shares and holdings.positions["22"].shares > 0


def test_no_session_and_unsupported_maker_never_prove_fills(db):
    session, _ = db
    request = OrderPlacementRequest(user_id=1, token_id="11", price=0.4, size=25, side="BUY")
    result = run(PaperExecutionEngine().place_order(request))
    assert result.status == "unfilled"
    result = paper.submit_paper_order(
        session,
        user_id=1,
        token_id="11",
        side="BUY",
        amount=10,
        strategy="market_maker",
        intent_key="maker",
    )
    assert result["reason"] == "MAKER_FLOW_AND_QUEUE_EVIDENCE_REQUIRED"
    assert session.query(UserTrade).count() == 0


def test_paper_realized_profits_never_enter_live_execution_metrics(db):
    from app.services.analytics.execution_analytics import get_execution_summary, get_trade_details

    session, clock = db
    order(session)
    clock[0] += timedelta(seconds=1)
    evidence(session, clock, bid="0.70", ask="0.71")
    order(session, side="SELL", amount="10", key="profit")
    summary = get_execution_summary(session, 1)
    assert summary["totals"]["gross_realized_pnl"] == 0
    assert summary["totals"]["submissions"] == 0
    assert summary["data_quality"]["paper_trades_excluded"] == 2
    details = get_trade_details(session, 1)
    assert all(row["execution_quality"] == "modeled_not_exchange_confirmed" for row in details)
