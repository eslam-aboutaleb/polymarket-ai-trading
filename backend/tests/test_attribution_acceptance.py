"""Financial identities, lineage and point-in-time benchmark acceptance for #3."""

import json
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from paper_account_fixtures import CID, evidence
from test_live_account_reconciliation import T0, client, snapshot
from test_live_account_reconciliation import account_db as _account_db

from app.models import Account, AppEvent, LiveOrder, UserTrade
from app.services.analytics.attribution_benchmarks import benchmark_fill, mark_observation
from app.services.analytics.live_account_reconciliation import build_reconciliation_report
from app.services.analytics.pnl_reconciliation import reconcile_confirmed_cashflows

account_db = _account_db


def confirmed(db, key, side, *, decision, matched, shares="10", notional="4", fee="0"):
    trade = UserTrade(
        user_id=1,
        account_id=11,
        market_id=CID,
        token_id="11",
        action=side.lower(),
        amount=float(notional),
        price=float(Decimal(notional) / Decimal(shares)),
        status="executed",
        strategy_source="manual",
        executed_at=matched,
    )
    db.add(trade)
    db.flush()
    order = LiveOrder(
        user_id=1,
        account_id=11,
        trade_id=trade.id,
        idempotency_key=key,
        fingerprint=key * 64,
        strategy="manual",
        token_id="11",
        side=side,
        price=Decimal(notional) / Decimal(shares),
        shares=Decimal(shares),
        notional=Decimal(notional),
        reserved=0,
        state="FILLED",
        order_hash=key,
        created_at=decision,
        response={"settled": True},
    )
    db.add(order)
    intent = AppEvent(
        user_id=1,
        account_id=11,
        source="live_gateway",
        aggregate_type="order",
        aggregate_id=key,
        event_type="order_intent_created",
        occurred_at=decision,
        payload_json=json.dumps(
            {"token_id": "11", "side": side, "strategy": "manual", "condition_id": CID}
        ),
    )
    db.add(intent)
    db.flush()
    event = AppEvent(
        user_id=1,
        account_id=11,
        source="live_gateway",
        aggregate_type="order",
        aggregate_id=key,
        event_type="order_fill_confirmed",
        occurred_at=matched + timedelta(milliseconds=1),
        payload_json=json.dumps(
            {
                "matched_at": matched.isoformat(),
                "trade_id": key,
                "transaction_hash": key,
                "shares": shares,
                "notional": notional,
                "fee_usdc": fee,
                "confirmed_rebate_usdc": "0",
            }
        ),
    )
    db.add(event)
    db.commit()
    return event


def test_cross_window_traceable_fifo(account_db):
    db = account_db
    buy = confirmed(db, "b", "BUY", decision=T0, matched=T0 + timedelta(seconds=1), fee=".1")
    opening = snapshot(db, T0 + timedelta(seconds=5), "95.9", {"11": "10"}, buy.id)
    sell = confirmed(
        db,
        "s",
        "SELL",
        decision=T0 + timedelta(seconds=6),
        matched=T0 + timedelta(seconds=7),
        notional="6",
        fee=".2",
    )
    closing = snapshot(db, T0 + timedelta(seconds=10), "101.7", {}, sell.id)
    report = build_reconciliation_report(db, db.get(Account, 11), opening, closing)
    assert report["gross_realized_pnl"] == "2.0", report["reasons"]
    assert Decimal(report["net_realized_pnl"]) == Decimal("1.7")
    assert Decimal(report["cash_residual_usdc"]) == 0
    assert report["position_residuals"]["11"] == "0"
    assert report["history_source_ids"] == [buy.id]
    assert report["source_ids"] == [sell.id]
    allocation = report["realized_allocations"][0]
    assert allocation["entry_id"] == buy.id and allocation["exit_source_id"] == sell.id
    assert Decimal(allocation["entry_fee"]) + Decimal(allocation["exit_fee"]) == Decimal(".3")
    assert not report["acceptance_complete"]
    assert report["verified_net_realized_pnl"] is None
    assert report["confidence"]["release_decision"] == "ABSTAIN_NO_FORWARD_VALIDATION"


def test_duplicate_fill_does_not_duplicate_benchmark_or_cash_and_conflict_rejected(account_db):
    db = account_db
    opening = snapshot(db, T0, "100", {})
    fill = confirmed(db, "b", "BUY", decision=T0, matched=T0 + timedelta(seconds=1))
    duplicate = AppEvent(
        user_id=1,
        account_id=11,
        source=fill.source,
        aggregate_type="order",
        aggregate_id=fill.aggregate_id,
        event_type=fill.event_type,
        occurred_at=T0 + timedelta(seconds=3),
        payload_json=fill.payload_json,
    )
    db.add(duplicate)
    db.commit()
    closing = snapshot(db, T0 + timedelta(seconds=10), "96", {"11": "10"}, duplicate.id)
    report = build_reconciliation_report(db, db.get(Account, 11), opening, closing)
    assert len(report["benchmarks"]) == len(report["ledger_rows"]) == 1
    assert report["source_ids"] == [fill.id]
    duplicate.payload_json = duplicate.payload_json.replace('"notional": "4"', '"notional": "5"')
    db.commit()
    response = client(db).get(
        "/api/trades/analytics/reconciliation",
        params={"opening_id": opening, "closing_id": closing},
        headers={"X-Account-Id": "11"},
    )
    assert response.status_code == 422 and "CONFLICTING_FILL_IDENTITY" in response.text


def test_native_benchmark_is_asof_and_spread_plus_execution_equals_shortfall(account_db):
    db = account_db
    captured = evidence(db, [T0], bid=".39", ask=".41")
    order = SimpleNamespace(token_id="11", side="BUY", created_at=T0 + timedelta(seconds=1))
    fill = {"source_id": 1, "shares": "10", "notional": "4.2"}
    result = benchmark_fill(db, order, fill, condition_id=CID)
    assert result["capture_id"] == captured.id
    assert result["implementation_shortfall"] == Decimal(".2")
    assert (
        result["spread_cost"] + result["execution_difference"] == result["implementation_shortfall"]
    )
    first_id = result["benchmark_id"]
    evidence(db, [T0 + timedelta(seconds=2)], bid=".7", ask=".8")
    assert benchmark_fill(db, order, fill, condition_id=CID)["benchmark_id"] == first_id
    order.created_at = T0 - timedelta(seconds=1)
    assert benchmark_fill(db, order, fill, condition_id=CID)["status"] == "abstain"
    order.created_at = T0 + timedelta(seconds=100)
    assert benchmark_fill(db, order, fill, condition_id=CID)["reason"] == "STALE_BOOK"


def test_marks_use_actual_remaining_basis_and_never_invent_fee_or_exit_fills(account_db):
    db = account_db
    evidence(db, [T0], bid=".6", ask=".7")
    mark = {"observed_at": T0.isoformat(), "cash_usdc": "96", "positions": {"11": "10"}}
    lots = {
        "11": [
            {
                "entry_source_id": 7,
                "strategy": "copy",
                "shares": "10",
                "entry_price": ".4",
                "net_cost_per_share": ".01",
            }
        ]
    }
    result = mark_observation(db, mark, lots)
    assert result["observed_nav"] == 102 and result["unrealized_pnl"] == Decimal("1.9")
    assert result["strategies"]["copy"] == result["unrealized_pnl"]
    assert result["quality"] == "indicative_bid_not_liquidation_value"
    lots["11"][0]["net_cost_per_share"] = None
    result = mark_observation(db, mark, lots)
    assert result["gross_unrealized_pnl"] == 2 and result["unrealized_pnl"] is None
    result = mark_observation(db, mark, {})
    assert result["observed_nav"] is None


def test_opening_lots_must_match_observed_quantity():
    opening = {"cash_usdc": "96", "positions": {"11": "11"}, "coverage": "complete_wallet"}
    lots = {
        "11": [
            {
                "entry_source_id": 1,
                "strategy": "copy",
                "shares": "10",
                "entry_price": ".4",
                "net_cost_per_share": "0",
            }
        ]
    }
    with pytest.raises(ValueError, match="QUANTITY_MISMATCH"):
        reconcile_confirmed_cashflows([], opening, opening, opening_lots=lots)


@pytest.mark.parametrize("quantity", [".000001", ".01", "1", "10", "1000000"])
@pytest.mark.parametrize("entry,exit_price", [(".1", ".9"), (".9", ".1"), (".5", ".5")])
def test_fifo_partition_and_strategy_additivity_across_size_and_price_boundaries(
    quantity, entry, exit_price
):
    from test_live_account_reconciliation import fill, observation

    q, buy, sell = map(Decimal, (quantity, entry, exit_price))
    rows = [
        fill(1, "BUY", quantity, str(q * buy), strategy="copy"),
        fill(2, "SELL", str(q / 2), str(q / 2 * sell), strategy="manual"),
        fill(3, "SELL", str(q / 2), str(q / 2 * sell), strategy="stop_loss"),
    ]
    report = reconcile_confirmed_cashflows(
        rows, observation("10000000"), observation(str(10000000 + q * (sell - buy)))
    )
    assert report["net_realized_pnl"] == q * (sell - buy)
    assert (
        sum(row["net_realized"] for row in report["strategies"].values())
        == report["net_realized_pnl"]
    )
    assert not report["closing_lots"]
    assert report["cash_residual_usdc"] == 0
    assert (
        sum(row["gross_cashflow"] for row in report["ledger_rows"])
        == report["gross_trading_cashflow"]
    )


def test_zero_fill_report_preserves_zero_activity_and_unknown_wallet_coverage(account_db):
    db = account_db
    opening = snapshot(db, T0, "100", {})
    closing = snapshot(db, T0 + timedelta(seconds=10), "100", {})
    report = build_reconciliation_report(db, db.get(Account, 11), opening, closing)
    assert report["source_ids"] == [] and report["benchmarks"] == []
    assert Decimal(report["gross_realized_pnl"]) == Decimal(report["net_realized_pnl"]) == 0
    assert report["verified_net_realized_pnl"] is None
    assert (
        not report["acceptance_complete"] and report["confidence"]["comparable_sample_count"] == 0
    )


def test_changed_order_or_missing_intent_cannot_relabel_provenance(account_db):
    db = account_db
    opening = snapshot(db, T0, "100", {})
    fill = confirmed(db, "b", "BUY", decision=T0, matched=T0 + timedelta(seconds=1))
    closing = snapshot(db, T0 + timedelta(seconds=10), "96", {"11": "10"}, fill.id)
    db.query(LiveOrder).one().strategy = "copy"
    db.commit()
    report = build_reconciliation_report(db, db.get(Account, 11), opening, closing)
    assert report["gross_realized_pnl"] is None
    assert "ORDER_INTENT_IDENTITY_CHANGED" in report["reasons"]


def test_venue_resolution_never_becomes_redeemed_cash_or_realized_profit(account_db):
    from app.services.analytics.attribution_benchmarks import settlement_observation

    evidence(account_db, [T0], bid=".39", ask=".4")
    evidence(account_db, [T0 + timedelta(seconds=1)], final="11")
    result = settlement_observation(
        account_db,
        {"observed_at": (T0 + timedelta(seconds=2)).isoformat(), "positions": {"11": "10"}},
    )
    row = result["positions"][0]
    assert row["status"] == "venue_final_unredeemed"
    assert Decimal(row["unredeemed_payout"]) == 10
    assert len(row["source_ids"]) == 2 and row["cash_credit"] is None
    assert not result["chain_final_redemption_verified"]


def test_saved_report_is_idempotent_owned_and_integrity_checked(account_db):
    from app.models.analytics.live_balance_snapshot import LiveAttributionReport

    db = account_db
    opening = snapshot(db, T0, "100", {})
    closing = snapshot(db, T0 + timedelta(seconds=10), "100", {})
    http = client(db)
    body = {"opening_id": opening, "closing_id": closing}
    saved = http.post(
        "/api/trades/analytics/reconciliation-reports", json=body, headers={"X-Account-Id": "11"}
    )
    assert saved.status_code == 200
    artifact = saved.json()
    assert (
        http.post(
            "/api/trades/analytics/reconciliation-reports",
            json=body,
            headers={"X-Account-Id": "11"},
        ).json()
        == artifact
    )
    path = "/api/trades/analytics/reconciliation-reports/" + artifact["report_id"]
    assert http.get(path, headers={"X-Account-Id": "11"}).json() == artifact
    db.add(Account(id=12, owner_user_id=1, name="Separate", kind="sub"))
    db.commit()
    assert http.get(path, headers={"X-Account-Id": "12"}).status_code == 404
    assert db.query(LiveAttributionReport).count() == 1
    db.get(LiveAttributionReport, artifact["report_id"]).report_json = "{}"
    db.commit()
    assert http.get(path, headers={"X-Account-Id": "11"}).status_code == 422
