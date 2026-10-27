from datetime import timedelta
from decimal import Decimal

import pytest
from book_research_fixtures import CID, NOW
from book_research_fixtures import research_db as _research_db

from app.config import get_settings
from app.models import MarketMakerConfig, PaperAccount, PaperMakerFill, PaperMakerOrder, UserTrade
from app.services.analytics import paper_accounting as paper
from app.services.trading import paper_maker_runtime as runtime
from app.services.trading.paper_maker_lifecycle import FlowEvent

research_db = _research_db
QUOTES = [{"token_id": "11", "side": "BUY", "price": "0.40", "size": "4"}]


@pytest.fixture
def maker(research_db, monkeypatch):
    db = research_db
    for key in ("paper_maker_fill_model_v2", "paper_journal_v2_enabled"):
        monkeypatch.setattr(get_settings(), key, True)
    monkeypatch.setattr(get_settings(), "paper_maker_queue_ahead", Decimal("3"))
    config = MarketMakerConfig(
        id=1,
        user_id=1,
        account_id=1,
        enabled=True,
        condition_id=CID,
        token_id_yes="11",
        token_id_no="22",
    )
    db.add(config)
    paper.configure_account(db, 1, 1, fee_bps=100, funding=1000, acknowledge_legacy=True)
    db.commit()
    return db, config


def flow(**changes):
    return FlowEvent(
        **{
            "id": "flow-a",
            "token_id": "11",
            "condition_id": CID,
            "occurred_at": NOW + timedelta(seconds=1),
            "available_at": NOW + timedelta(seconds=2),
            "price": Decimal("0.40"),
            "shares": Decimal("8"),
            "aggressor": "SELL",
            "continuous": True,
            "evidence_snapshot_id": "synthetic-attributed-flow",
            **changes,
        }
    )


def test_hundred_syncs_preserve_order_and_reservation_without_fill(maker):
    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    identifier = db.query(PaperMakerOrder).one().id
    for _ in range(99):
        runtime.sync(db, config, QUOTES, NOW)
    assert db.query(PaperMakerOrder).count() == 1
    assert db.query(PaperMakerOrder).one().id == identifier
    ledger = paper.restore(db, db.get(PaperAccount, 1))
    assert ledger.cash == 1000 and ledger.reserved_cash == Decimal("4.04")
    assert not db.query(PaperMakerFill).count() and not db.query(UserTrade).count()


def test_partial_fill_restart_dedup_cancel_reconciles_exactly(maker):
    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    state = db.get(PaperAccount, 1)
    ledger = paper.restore(db, state)
    runtime.consume(db, state, ledger, [flow()], NOW + timedelta(seconds=3))
    db.commit()
    db.expire_all()
    ledger = paper.restore(db, state)
    assert ledger.cash == Decimal("997.98") and ledger.positions["11"].shares == 5
    assert ledger.reserved_cash == Decimal("2.02")
    runtime.consume(db, state, ledger, [flow()], NOW + timedelta(seconds=3))
    assert db.query(PaperMakerFill).count() == 1
    runtime.cancel_config(db, config, NOW + timedelta(seconds=4))
    ledger = paper.restore(db, state)
    assert ledger.cash == Decimal("997.98") and ledger.reserved_cash == 0
    assert db.query(PaperMakerOrder).one().state == "CANCELLED"
    runtime.sync(db, config, QUOTES, NOW + timedelta(seconds=4))
    assert db.query(PaperMakerOrder).count() == 2  # terminal IDs are not resurrected


def test_rolls_back_fill_state_and_journal_together(maker):
    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    db.commit()
    state = db.get(PaperAccount, 1)
    with pytest.raises(RuntimeError), paper.transaction(db):
        ledger = paper.restore(db, state)
        runtime.consume(db, state, ledger, [flow()], NOW + timedelta(seconds=3))
        raise RuntimeError("crash before commit")
    db.expire_all()
    assert db.query(PaperMakerFill).count() == 0
    assert db.query(PaperMakerOrder).one().cumulative_filled == 0
    assert paper.restore(db, state).cash == 1000


def test_missing_fees_inventory_tick_and_latency(maker, monkeypatch):
    db, config = maker
    assert runtime.sync(db, config, [{**QUOTES[0], "side": "SELL"}], NOW)["intents_created"] == 0
    assert runtime.sync(db, config, [{**QUOTES[0], "price": "0.401"}], NOW)["intents_created"] == 0
    monkeypatch.setattr(get_settings(), "paper_maker_latency_ms", 5000)
    runtime.sync(db, config, QUOTES, NOW)
    state = db.get(PaperAccount, 1)
    assert not runtime.consume(
        db, state, paper.restore(db, state), [flow()], NOW + timedelta(seconds=3)
    )
    runtime.cancel_config(db, config, NOW + timedelta(seconds=4))
    state.fee_bps = None
    db.flush()
    assert (
        "UNKNOWN_FEE_POLICY"
        in runtime.sync(db, config, QUOTES, NOW + timedelta(seconds=4))["reasons"]
    )


def test_native_trade_side_is_not_invented_aggressor_evidence(maker):
    from app.services.market_data.venue_observations import append_event

    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    append_event(
        db,
        {
            "event_type": "last_trade_price",
            "market": CID,
            "asset_id": "11",
            "timestamp": str(int((NOW + timedelta(seconds=1)).timestamp() * 1000)),
            "price": "0.40",
            "size": "100",
            "side": "SELL",
        },
        received_at=NOW + timedelta(seconds=2),
        connection_id="native-test",
    )
    runtime.sync(db, config, QUOTES, NOW + timedelta(seconds=3))
    assert db.query(PaperMakerFill).count() == 0
    assert paper.restore(db, db.get(PaperAccount, 1)).cash == 1000


def test_fixed_horizon_drift_freezes_unavailable_without_future_leakage(maker):
    from app.models import PaperMakerMark

    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    state = db.get(PaperAccount, 1)
    runtime.consume(db, state, paper.restore(db, state), [flow()], NOW + timedelta(seconds=3))
    runtime.capture_drift(db, state, NOW + timedelta(seconds=8))
    row = db.query(PaperMakerMark).one()
    assert row.horizon_seconds == 5
    assert '"markout_per_share": null' in row.payload_json
    original = row.payload_json
    runtime.capture_drift(db, state, NOW + timedelta(seconds=100))
    assert db.query(PaperMakerMark).count() == 3
    assert db.query(PaperMakerMark).filter_by(horizon_seconds=5).one().payload_json == original


def test_config_cap_and_shadow_report_do_not_count_quotes_as_execution(maker):
    from app.models import Account

    db, config = maker
    config.max_collateral = 3
    assert "CONFIG_COLLATERAL_CAP" in runtime.sync(db, config, QUOTES, NOW)["reasons"]
    config.max_collateral = 100
    runtime.sync(db, config, QUOTES, NOW)
    result = runtime.report(db, config, db.get(Account, 1))
    assert Decimal(result["quoted_notional_usdc"]) == 4
    assert Decimal(result["executed_notional_usdc"]) == 0
    assert result["intents"][0]["source_as_of"] == NOW.isoformat()
    assert result["legacy_excluded_from_decision_grade"]


def test_paper_path_never_reaches_execution_or_approval(maker, monkeypatch):
    import asyncio

    from app.services.trading import market_maker_service as service

    db, config = maker

    def forbidden(*args, **kwargs):
        raise AssertionError("paper reached live mutation")

    monkeypatch.setattr(service, "_place_maker_order", forbidden)
    monkeypatch.setattr(service, "_cancel_owned_maker_quotes", forbidden)
    for package in ("py_clob_client.client", "py_clob_client_v2.client"):
        import importlib

        client = importlib.import_module(package).ClobClient
        for method in (
            "create_order",
            "post_order",
            "cancel",
            "cancel_all",
            "cancel_order",
            "cancel_orders",
            "create_market_order",
            "update_balance_allowance",
        ):
            if hasattr(client, method):
                monkeypatch.setattr(client, method, forbidden)
    result = asyncio.run(service._sync_market_maker_paper(db, config, 0.45, QUOTES, {}))
    assert result["placed"] == 0 and result["simulated"]
    assert db.query(UserTrade).count() == 0


def test_reprice_cancel_latency_keeps_reservation_until_effective_boundary(maker, monkeypatch):
    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    first_id = db.query(PaperMakerOrder).one().id
    monkeypatch.setattr(get_settings(), "paper_maker_latency_ms", 5000)
    runtime.sync(db, config, [{**QUOTES[0], "price": "0.39"}], NOW + timedelta(seconds=1))
    old = db.get(PaperMakerOrder, first_id)
    assert old.state == "CANCEL_REQUESTED"
    races_before = runtime.metrics["cancel_race_fills"]
    state = db.get(PaperAccount, 1)
    ledger = paper.restore(db, state)
    runtime.consume(db, state, ledger, [flow()], NOW + timedelta(seconds=3))
    assert old.cumulative_filled == 5
    assert runtime.metrics["cancel_race_fills"] == races_before + 1
    runtime.consume(db, state, ledger, [], NOW + timedelta(seconds=6))
    assert old.state == "CANCELLED"
    assert f"maker-reserve:{first_id}" not in ledger.orders


def test_depth_and_rule_receipts_are_pinned_against_retention(maker):
    from app.models.market_data.venue_observation import VenueObservation
    from app.services.market_data.venue_observations import prune_unpinned

    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    row = db.query(PaperMakerOrder).one()
    prune_unpinned(db, NOW + timedelta(days=1))
    db.flush()
    assert db.get(VenueObservation, row.source_snapshot_id) is not None
    assert db.get(VenueObservation, row.metadata_snapshot_id) is not None


def test_account_print_budget_shared_across_configurations(maker):
    db, config = maker
    second = MarketMakerConfig(
        id=2,
        user_id=1,
        account_id=1,
        enabled=True,
        condition_id=CID,
        token_id_yes="11",
        token_id_no="22",
    )
    db.add(second)
    db.flush()
    runtime.sync(db, config, QUOTES, NOW)
    runtime.sync(db, second, QUOTES, NOW)
    state = db.get(PaperAccount, 1)
    fills = runtime.consume(
        db,
        state,
        paper.restore(db, state),
        [flow(shares=Decimal("15"))],
        NOW + timedelta(seconds=3),
    )
    assert sum(f.shares for f in fills) <= 15
    assert (
        sum(f.shares for f in fills) == 10
    )  # first queue consumes three; second consumes the last two


def test_fresh_postfill_snapshot_produces_frozen_indicative_markout(maker, monkeypatch):
    import asyncio
    import json

    import httpx
    from book_research_fixtures import raw_book, venue_handler

    from app.models import PaperMakerMark
    from app.services.market_data import book_research, replay_capture
    from app.services.market_data.replay_capture import PublicVenueCapture

    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    state = db.get(PaperAccount, 1)
    runtime.consume(db, state, paper.restore(db, state), [flow()], NOW + timedelta(seconds=3))
    target = NOW + timedelta(seconds=8)
    monkeypatch.setattr(replay_capture, "utc_now", lambda: target)

    def provider(request):
        if request.url.path.endswith("/book"):
            return httpx.Response(200, json=raw_book(request.url.params["token_id"], target))
        return venue_handler(request)

    async def seed():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            await book_research.capture_market_books(db, CID, PublicVenueCapture(client))

    asyncio.run(seed())
    runtime.capture_drift(db, state, target)
    result = json.loads(db.query(PaperMakerMark).one().payload_json)
    assert Decimal(result["markout_per_share"]) == Decimal("0.05")
    assert result["quality"] == "indicative_midpoint_not_realized_pnl"


def test_legacy_backfill_marks_sidecar_without_modifying_original_trade(maker):
    from app.models import PaperMakerLegacy

    db, _ = maker
    trade = UserTrade(
        user_id=1,
        account_id=1,
        market_id=CID,
        action="buy",
        amount=10,
        price=0.4,
        status="simulated",
        strategy_source="market_maker",
        calculation_details='{"old": true}',
    )
    db.add(trade)
    db.flush()
    original = trade.calculation_details
    state = db.get(PaperAccount, 1)
    assert runtime.classify_legacy(db, state) == 1
    assert runtime.classify_legacy(db, state) == 0
    assert db.query(PaperMakerLegacy).one().quality == "legacy_unverified_fill"
    assert trade.calculation_details == original and trade.price == 0.4 and trade.amount == 10


def test_stop_without_inprocess_worker_releases_durable_reservations(maker):
    from app.services.trading.market_maker_service import set_market_maker_config_state

    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    db.commit()
    set_market_maker_config_state(db, config, enabled=False)
    ledger = paper.restore(db, db.get(PaperAccount, 1))
    assert ledger.reserved_cash == 0 and ledger.cash == 1000
    assert db.query(PaperMakerOrder).one().state == "CANCELLED"


def test_invalid_or_halted_book_cancels_without_creating_fill(maker, monkeypatch):
    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    load = runtime.load_analysis

    def halted(db, query):
        result, evidence = load(db, query)
        result["spec"]["active"] = False
        return result, evidence

    monkeypatch.setattr(runtime, "load_analysis", halted)
    runtime.sync(db, config, QUOTES, NOW + timedelta(seconds=1))
    assert db.query(PaperMakerOrder).one().state == "CANCELLED"
    ledger = paper.restore(db, db.get(PaperAccount, 1))
    assert ledger.reserved_cash == 0 and ledger.cash == 1000
    assert not db.query(PaperMakerFill).count()


def test_edit_or_disable_after_restart_does_not_leave_old_reservations(maker):
    from app.services.trading.market_maker_service import upsert_market_maker_config

    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    db.commit()
    upsert_market_maker_config(
        db, 1, {"condition_id": CID, "enabled": False, "band_order_size": 20}
    )
    assert db.query(PaperMakerOrder).one().state == "CANCELLED"
    assert paper.restore(db, db.get(PaperAccount, 1)).reserved_cash == 0


def test_sync_rechecks_disabled_config_under_account_lock(maker):
    from sqlalchemy.orm import Session

    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    db.commit()
    assert config.enabled
    with Session(db.get_bind()) as external:
        external.get(MarketMakerConfig, config.id).enabled = False
        external.commit()
    result = runtime.sync(db, config, QUOTES, NOW + timedelta(seconds=1))
    assert result["reasons"] == ["CONFIG_DISABLED"]
    assert db.query(PaperMakerOrder).one().state == "CANCELLED"
    assert paper.restore(db, db.get(PaperAccount, 1)).reserved_cash == 0


def test_shared_account_latency_and_lot_policy_cannot_be_bypassed(maker):
    db, config = maker
    paper.configure_account(db, 1, 1, policy={"latency_ms": 5000, "lot_size": "0.1"})
    runtime.sync(db, config, [{**QUOTES[0], "size": "4.01"}], NOW)
    row = db.query(PaperMakerOrder).one()
    assert row.shares == Decimal(10)
    state = db.get(PaperAccount, 1)
    assert not runtime.consume(
        db, state, paper.restore(db, state), [flow()], NOW + timedelta(seconds=3)
    )


def test_marketable_quotes_are_not_mislabeled_resting(maker):
    db, config = maker
    result = runtime.sync(db, config, [{**QUOTES[0], "price": "0.50"}], NOW)
    assert result["reasons"] == ["MARKETABLE_QUOTE_CANNOT_REST"]
    assert db.query(PaperMakerOrder).count() == 0
    assert paper.restore(db, db.get(PaperAccount, 1)).reserved_cash == 0


def test_zero_age_account_policy_cancels_before_consuming_flow(maker, monkeypatch):
    db, config = maker
    runtime.sync(db, config, QUOTES, NOW)
    paper.configure_account(db, 1, 1, policy={"max_book_age_seconds": 0})

    def forbidden(*args, **kwargs):
        raise AssertionError("stale book reached fill consumption")

    monkeypatch.setattr(runtime, "_consume_archive", forbidden)
    runtime.sync(db, config, QUOTES, NOW + timedelta(seconds=1))
    assert db.query(PaperMakerOrder).one().state == "CANCELLED"
    assert paper.restore(db, db.get(PaperAccount, 1)).reserved_cash == 0
