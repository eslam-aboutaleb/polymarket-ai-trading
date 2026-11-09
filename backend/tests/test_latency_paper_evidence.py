import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from latency_paper_fixtures import native
from paper_account_fixtures import CID
from paper_account_fixtures import db as _db

from app.models import UserSettings
from app.models.trading.paper_account import PaperOrderRecord
from app.models.trading.paper_execution_intent import PaperExecutionIntent
from app.models.trading.user_trade import UserTrade
from app.services.analytics import paper_accounting as paper
from app.services.trading import latency_paper as runtime

db = _db


def opportunity(clock):
    return SimpleNamespace(
        market=SimpleNamespace(condition_id=CID),
        window_start_epoch=123,
        detected_at=clock[0],
        feed_lag_ms=0,
        p_model=0.9,
        side="up",
    )


def run(db_clock, monkeypatch, delay=300, capture=None, account=1):
    session, clock = db_clock
    op = opportunity(clock)
    settings = session.query(UserSettings).filter_by(user_id=1).one()
    monkeypatch.setattr(runtime, "utc_now", lambda: clock[0])

    async def sleep(seconds):
        assert session.query(PaperExecutionIntent).count() == 1
        clock[0] += timedelta(seconds=seconds)

    async def refresh(*_, **__):
        if delay == 0:
            clock[0] += timedelta(milliseconds=1)
        if capture:
            capture(session, clock)
        return bool(capture)

    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    monkeypatch.setattr(runtime, "refresh_evidence", refresh)
    return asyncio.run(
        runtime.execute(
            session, settings, op, "11", 10, delay, account_id=account, minimum_edge=0.03
        )
    )


@pytest.mark.parametrize("delay", [0, 300, 1500])
def test_measured_delay_native_depth_is_estimated(db, monkeypatch, delay):
    result = run(db, monkeypatch, delay, native)
    assert result["execution_state"] == "ESTIMATED", result
    assert result["exchange_confirmed"] is False
    assert Decimal(result["shares"]) == 20
    assert result["replay_id"]
    assert db[0].query(UserTrade).one().pnl is None


def test_missing_native_book_never_fabricates_trade(db, monkeypatch):
    result = run(db, monkeypatch)
    assert result["execution_state"] == "UNVERIFIABLE_DATA"
    assert db[0].query(UserTrade).count() == 0
    assert db[0].query(PaperOrderRecord).count() == 1
    assert paper.restore(db[0], paper.account_state(db[0], 1, 1)).cash == 1000


@pytest.mark.parametrize(
    "depth,state,shares",
    [("0", "UNVERIFIABLE_DATA", 0), ("5", "PARTIALLY_FILLED_MODELED", 5), ("100", "ESTIMATED", 20)],
)
def test_depth_bound(db, monkeypatch, depth, state, shares):
    result = run(db, monkeypatch, capture=lambda s, c: native(s, c, depth=depth))
    assert result["execution_state"] == state, result
    assert Decimal(result["shares"]) == shares


@pytest.mark.parametrize("delay", [300, 1500])
def test_adverse_book_after_delay_cannot_use_trigger_quote(db, monkeypatch, delay):
    result = run(db, monkeypatch, delay, lambda s, c: native(s, c, ask=".90", bid=".89"))
    assert result["reason"] == "EDGE_NOT_EXECUTABLE_AFTER_COSTS"
    assert result["execution_state"] == "UNFILLED"
    assert db[0].query(UserTrade).count() == 0


@pytest.mark.parametrize("case", ["fees", "policy", "halt", "mode", "subcap"])
def test_changes_during_capture_cannot_relax_frozen_request(db, monkeypatch, case):
    def changed(session, clock):
        native(session, clock)
        if case == "fees":
            paper.configure_account(session, 1, 1, fee_bps=100)
        elif case == "policy":
            paper.configure_account(session, 1, 1, policy={"latency_ms": 0, "lot_size": ".1"})
        elif case == "subcap":
            from app.models import AccountSettings

            session.query(AccountSettings).one().max_position_size = 1
        else:
            settings = session.query(UserSettings).one()
            if case == "halt":
                settings.trading_halted = True
            else:
                settings.simulation_mode = False
        session.commit()

    result = run(db, monkeypatch, capture=changed, account=2 if case == "subcap" else 1)
    assert result["execution_state"] == "UNVERIFIABLE_DATA", result
    assert db[0].query(UserTrade).count() == 0


@pytest.mark.parametrize("case", ["stale", "future", "crossed", "resolved"])
def test_invalid_evidence_abstains(db, monkeypatch, case):
    def capture(session, clock):
        kwargs = {}
        if case == "stale":
            kwargs["source_at"] = clock[0] - timedelta(seconds=100)
        if case == "future":
            native(session, [clock[0] + timedelta(seconds=1)])
            return
        if case == "crossed":
            kwargs["bid"] = ".50"
        if case == "resolved":
            kwargs["final"] = "11"
        native(session, clock, **kwargs)

    result = run(db, monkeypatch, capture=capture)
    assert result["execution_state"] == "UNVERIFIABLE_DATA", result
    assert db[0].query(UserTrade).count() == 0


def test_retry_reuses_exact_result_and_single_journal(db, monkeypatch):
    result = run(db, monkeypatch, capture=native)
    session, clock = db
    frozen = session.query(PaperExecutionIntent).one().request_json
    clock[0] += timedelta(seconds=2)
    repeated = run(db, monkeypatch, capture=native)
    assert repeated == result
    assert session.query(UserTrade).count() == session.query(PaperOrderRecord).count() == 1
    assert session.query(PaperExecutionIntent).one().request_json == frozen


def test_cancellation_preserves_intent_then_expiry_abstains(db, monkeypatch):
    session, clock = db
    monkeypatch.setattr(runtime, "utc_now", lambda: clock[0])

    async def cancel(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(runtime.asyncio, "sleep", cancel)
    settings = session.query(UserSettings).one()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime.execute(session, settings, opportunity(clock), "11", 10, 300))
    assert session.query(PaperExecutionIntent).count() == 1
    assert session.query(UserTrade).count() == 0
    clock[0] += timedelta(seconds=2)
    result = run(db, monkeypatch, capture=native)
    assert result["reason"] == "INTENT_EXPIRED"
    assert result["execution_state"] == "UNVERIFIABLE_DATA"


def test_conflicting_pending_intent_cannot_poison_original(db, monkeypatch):
    session, clock = db
    monkeypatch.setattr(runtime, "utc_now", lambda: clock[0])
    settings = session.query(UserSettings).one()
    runtime.freeze(session, settings, opportunity(clock), "11", 10, 300, 1, 0.03)
    with pytest.raises(runtime.IntentConflict):
        asyncio.run(runtime.execute(session, settings, opportunity(clock), "22", 10, 300))
    assert session.query(PaperOrderRecord).count() == 0
    assert session.query(PaperExecutionIntent).count() == 1


def test_subaccount_config_changed_to_personal_cancels_pending(db, monkeypatch):
    from app.models.trading.latency_arb_config import LatencyArbConfig

    session, _ = db
    session.add(LatencyArbConfig(user_id=1, enabled=True, account_id=2))
    session.commit()

    def changed(session, clock):
        native(session, clock)
        session.query(LatencyArbConfig).one().account_id = None
        session.commit()

    result = run(db, monkeypatch, capture=changed, account=2)
    assert result["reason"] == "STRATEGY_DISABLED_OR_ACCOUNT_CHANGED"
    assert session.query(UserTrade).count() == 0


def test_owner_inventory_cap_includes_other_subaccounts(db, monkeypatch):
    session, _ = db
    paper.submit_paper_order(
        session,
        user_id=1,
        account_id=2,
        token_id="11",
        side="BUY",
        amount=10,
        strategy="manual",
        intent_key="other-account",
    )
    session.query(UserSettings).one().max_position_size = 15
    session.commit()
    result = run(db, monkeypatch, capture=native)
    assert result["reason"] == "PAPER_POSITION_LIMIT"
    assert session.query(UserTrade).count() == 1


def test_fee_greater_than_edge_abstains_and_fee_inclusive_cash_is_conserved(db, monkeypatch):
    session, _ = db
    paper.configure_account(session, 1, 1, fee_bps=10000)
    session.commit()
    result = run(db, monkeypatch, capture=native)
    assert result["reason"] == "EDGE_NOT_EXECUTABLE_AFTER_COSTS"
    assert session.query(UserTrade).count() == 0


@pytest.mark.parametrize("fee", [0, 100, 1000])
def test_decimal_cash_inventory_and_fees_are_conserved(db, monkeypatch, fee):
    session, _ = db
    paper.configure_account(session, 1, 1, fee_bps=fee)
    session.commit()
    result = run(db, monkeypatch, capture=native)
    cost = Decimal(result["notional_usdc"]) + Decimal(result["fee_usdc"])
    ledger = paper.restore(session, paper.account_state(session, 1, 1))
    assert 0 < cost <= 10
    assert ledger.cash + cost == Decimal(1000)
    assert ledger.positions["11"].shares == Decimal(result["shares"])
    assert ledger.available_cash >= 0


def test_daily_loss_counts_negative_events_even_after_wins(db, monkeypatch):
    from paper_account_fixtures import evidence, order

    session, clock = db
    order(session, amount="10", key="buy-profit")
    clock[0] += timedelta(seconds=1)
    evidence(session, clock, bid=".60", ask=".61")
    order(session, amount="10", side="SELL", key="realize-profit")
    order(session, amount="10", key="buy-loss")
    clock[0] += timedelta(seconds=1)
    evidence(session, clock, bid=".40", ask=".41")
    order(session, amount="10", side="SELL", key="realize-loss")
    # Profit +2 and loss -2.1: net loss .1, gross loss 2.1.
    session.query(UserSettings).one().daily_loss_limit = 11
    session.commit()
    result = run(db, monkeypatch, capture=native)
    assert result["reason"] == "PAPER_LOSS_LIMIT"
    assert session.query(UserTrade).count() == 4


def test_pre_delay_source_clock_is_not_execution_evidence(db, monkeypatch):
    result = run(
        db,
        monkeypatch,
        capture=lambda s, c: native(s, c, source_at=c[0] - timedelta(milliseconds=100)),
    )
    assert result["reason"] == "NO_POST_DELAY_BOOK"
    assert db[0].query(UserTrade).count() == 0


def test_ambiguous_same_receipt_clock_cannot_choose_a_favorable_book(db, monkeypatch):
    def capture(session, clock):
        native(session, clock)
        native(session, clock, ask=".60", bid=".59")

    result = run(db, monkeypatch, capture=capture)
    assert result["reason"] == "AMBIGUOUS_RECEIPT_ORDER"
    assert db[0].query(UserTrade).count() == 0


def test_unknown_fees_cannot_be_retroactively_improved_during_capture(db, monkeypatch):
    session, _ = db
    paper.account_state(session, 1, 1).fee_bps = None
    session.commit()

    def capture(session, clock):
        native(session, clock)
        paper.configure_account(session, 1, 1, fee_bps=0)
        session.commit()

    result = run(db, monkeypatch, capture=capture)
    assert result["reason"] == "PAPER_ASSUMPTIONS_CHANGED"
    assert session.query(UserTrade).count() == 0


def test_void_market_abstains_without_invented_settlement(db, monkeypatch):
    result = run(db, monkeypatch, capture=lambda s, c: native(s, c, final="void"))
    assert result["execution_state"] == "UNVERIFIABLE_DATA"
    assert Decimal(result["realized_pnl"]) == 0
    assert db[0].query(UserTrade).count() == 0
